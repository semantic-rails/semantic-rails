from __future__ import annotations

import hashlib
import json
from typing import Any

from ..ast import NormalizedQuery, normalize_query
from ..dialects import SqlDialect, dialect_for_warehouse
from ..errors import SemanticLayerError
from ..expressions import (
    AggregateExpr,
    ArithmeticExpr,
    BooleanExpr,
    CallExpr,
    CaseExpr,
    CaseWhenExpr,
    ColumnRefExpr,
    ComparisonExpr,
    ConditionalAggregateExpr,
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
    collect_column_refs,
    expr_to_dict,
    parse_semantic_expression,
)
from ..ir import BoundMeasure
from ..schema import MeasureConfig, PackageConfig
from ..sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCase,
    SqlCaseWhen,
    SqlIn,
    SqlLiteral,
)
from .indexes import (
    _default_temporal_role,
    _dimension_index,
    _entity_index,
    _measure_index,
    _recipe_index,
    _table_to_entity,
)
from .paths import _column_ref


def _freeze_payload(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _else_clause_collapses_to_no_else(else_expr: Any, aggregation: str) -> bool:
    """Decide whether an explicit ELSE branch is semantically equivalent
    to no ELSE for the given aggregation, so the single-branch CASE
    pattern can still be folded to the native conditional-aggregate.

    Equivalence rules (proof by case analysis):

    - ``ELSE NULL`` is equivalent to no ELSE for every aggregation that
      ignores NULLs (count, count_distinct, sum, avg, min, max, median,
      percentile — i.e. every aggregation we route through this hook).
      A NULL contribution is identical to the row being absent.

    - For SUM only, ``ELSE 0`` is also equivalent: 0 contributes nothing
      to a SUM result. This matches the mf2sr-generated ``sum_boolean``
      idiom (``SUM(CASE WHEN cond THEN 1 ELSE 0 END)``) and the
      count-fallback path in mf2sr/translate.py.

    - For COUNT, ``ELSE 0`` is NOT equivalent — ``COUNT(CASE WHEN cond
      THEN 1 ELSE 0 END)`` counts every row (0 is non-null), giving the
      total row count rather than the count of matching rows.

    - For AVG/MIN/MAX, ``ELSE 0`` would skew the result (zeros enter the
      population), so reject those too.
    """
    if else_expr is None:
        return True
    if not isinstance(else_expr, SqlLiteral):
        return False
    if else_expr.value is None:
        return True
    return aggregation.lower() == "sum" and else_expr.value == 0


def _maybe_conditional_aggregate(expr: Any, aggregation: str, dialect: SqlDialect) -> Any | None:
    """If ``expr`` is the canonical ``CASE WHEN cond THEN body END``
    shape that ``aggregate_if`` produces (one when, no else — or an
    ELSE branch that collapses to equivalent NULL/0 semantics for the
    aggregation), delegate to ``dialect.conditional_aggregate`` so
    dialects with a native form (Snowflake ``COUNT_IF`` / ``SUM_IF``,
    BigQuery ``COUNTIF``, Postgres ``FILTER (WHERE …)``) can emit it.
    Returns ``None`` if the pattern doesn't apply — the caller falls
    through to the generic ``<AGG>(<expr>)`` path. This optimisation is
    dialect-agnostic: the base ``SqlDialect.conditional_aggregate``
    re-emits the same CASE WHEN form, so dialects that don't override
    see no change.

    Patterns recognised:

    - ``<AGG>(CASE WHEN cond THEN body END)`` — the ``aggregate_if``
      shape.
    - ``<AGG>(CASE WHEN cond THEN body ELSE NULL END)`` — the
      jaffle_shop / hand-authored idiom (orders.yml uses this 4× to
      count rows where a flag is true, with body = key column and
      explicit ``else: literal null``).
    - ``SUM(CASE WHEN cond THEN body ELSE 0 END)`` — the mf2sr
      ``sum_boolean`` and count-fallback idiom (translate.py
      lines 498-512 and 562-582). COUNT with ``ELSE 0`` is *not*
      folded because it counts every row.
    """
    if not isinstance(expr, SqlCase):
        return None
    if len(expr.whens) != 1:
        return None
    if not _else_clause_collapses_to_no_else(expr.else_expr, aggregation):
        return None
    agg = aggregation.lower()
    only_when = expr.whens[0]
    condition = only_when.condition
    body = only_when.result
    if agg == "count":
        # ``aggregate_if(count, cond)`` lowers to body=Literal(1). Pass
        # value=None so dialect.conditional_aggregate emits the
        # parameter-less native form (COUNT_IF / COUNTIF). For any
        # other body (e.g. COUNT(CASE WHEN cond THEN col END) — a
        # COUNT-distinct-by-condition idiom) we still benefit by
        # routing through the hook with explicit value.
        if isinstance(body, SqlLiteral) and body.value == 1:
            return dialect.conditional_aggregate("count", condition, None)
        return dialect.conditional_aggregate("count", condition, body)
    if agg == "sum":
        return dialect.conditional_aggregate("sum", condition, body)
    # Other aggregations (avg/min/max/median/etc.) still flow through
    # the hook so dialects with PERCENTILE_CONT FILTER (WHERE…) etc.
    # can override later. The base implementation re-emits CASE WHEN.
    if agg in {"avg", "min", "max"}:
        return dialect.conditional_aggregate(agg, condition, body)
    return None


def _aggregation_expr(
    expr: Any,
    aggregation: str,
    *,
    order_expr: Any | None = None,
    parameters: dict[str, Any] | None = None,
    dialect: SqlDialect | None = None,
) -> Any:
    agg = aggregation.lower()
    params = dict(parameters or {})
    active_dialect = dialect or dialect_for_warehouse("")
    # If the inner expression is a single-branch CASE WHEN (the shape
    # ``aggregate_if`` produces, and that many handwritten measures use
    # too), delegate to the dialect's ``conditional_aggregate`` hook so
    # native forms (COUNT_IF / SUM_IF on Snowflake, COUNTIF on BigQuery,
    # FILTER (WHERE …) on Postgres) light up automatically.
    if agg in {"count", "sum", "avg", "min", "max"}:
        conditional = _maybe_conditional_aggregate(expr, agg, active_dialect)
        if conditional is not None:
            return conditional
    if agg == "sum":
        return SqlCall("SUM", [expr])
    if agg == "count":
        return SqlCall("COUNT", [expr])
    if agg == "count_distinct":
        return SqlCall("COUNT", [expr], distinct=True)
    if agg == "avg":
        return SqlCall("AVG", [expr])
    if agg == "min":
        return SqlCall("MIN", [expr])
    if agg == "max":
        return SqlCall("MAX", [expr])
    if agg == "median":
        return active_dialect.median(expr)
    if agg == "percentile":
        if "p" not in params:
            raise SemanticLayerError(
                "UNSUPPORTED_AGGREGATION",
                "percentile requires parameters.p",
                details={"aggregation": "percentile", "parameters_received": dict(params)},
            )
        try:
            percentile = float(params["p"])
        except (TypeError, ValueError) as exc:
            raise SemanticLayerError(
                "UNSUPPORTED_AGGREGATION",
                "percentile parameters.p must be numeric",
                details={"aggregation": "percentile", "parameters_received": dict(params)},
            ) from exc
        if percentile < 0 or percentile > 1:
            raise SemanticLayerError(
                "UNSUPPORTED_AGGREGATION",
                "percentile parameters.p must be between 0 and 1",
                details={"aggregation": "percentile", "parameters_received": dict(params)},
            )
        return active_dialect.percentile_cont(expr, percentile)
    if agg == "first_value":
        if order_expr is None:
            raise SemanticLayerError(
                "INVALID_TEMPORAL_ROLE", "first_value requires an ordering temporal role"
            )
        return active_dialect.first_value(expr, order_expr)
    if agg == "last_value":
        if order_expr is None:
            raise SemanticLayerError(
                "INVALID_TEMPORAL_ROLE", "last_value requires an ordering temporal role"
            )
        return active_dialect.last_value(expr, order_expr)
    raise SemanticLayerError(
        "UNSUPPORTED_AGGREGATION",
        f"Unsupported aggregation '{aggregation}'",
        details={"aggregation": str(aggregation or ""), "parameters_received": dict(params)},
    )


def _resolve_expr_entity(ref: ColumnRefExpr, measure: MeasureConfig, config: PackageConfig) -> str:
    if ref.entity:
        return ref.entity
    if ref.table:
        entity_id = _table_to_entity(config).get(ref.table)
        if entity_id:
            return entity_id
        raise SemanticLayerError(
            "INVALID_CONFIG", f"Unknown table reference '{ref.table}' in measure '{measure.id}'"
        )
    return measure.entity


def _measure_required_entities(measure: MeasureConfig, config: PackageConfig) -> set[str]:
    required = {measure.entity}
    for ref in collect_column_refs(measure.expr):
        required.add(_resolve_expr_entity(ref, measure, config))
    return required


def _config_expr_to_sql(expr: SemanticExpr, measure: MeasureConfig, config: PackageConfig) -> Any:
    entities = _entity_index(config)
    if isinstance(expr, ColumnRefExpr):
        entity_id = _resolve_expr_entity(expr, measure, config)
        # For a measure's own entity, prefer source_relation (fact table)
        # over entity.table (which is typically the calendar relation for
        # fact measures that bind to a time entity).
        source = getattr(measure, "source_relation", "") or ""
        if source and entity_id == measure.entity and not expr.table:
            return _column_ref(source, expr.column)
        return _column_ref(entities[entity_id].table, expr.column)
    if isinstance(expr, LiteralExpr):
        return SqlLiteral(expr.value)
    if isinstance(expr, ArithmeticExpr):
        op_map = {"add": "+", "subtract": "-", "multiply": "*", "divide": "/"}
        op = op_map.get(expr.op)
        if op is None:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST", f"Unsupported arithmetic op '{expr.op}'"
            )
        return SqlBinary(
            _config_expr_to_sql(expr.left, measure, config),
            op,
            _config_expr_to_sql(expr.right, measure, config),
        )
    if isinstance(expr, ComparisonExpr):
        return SqlBinary(
            _config_expr_to_sql(expr.left, measure, config),
            expr.op,
            _config_expr_to_sql(expr.right, measure, config),
        )
    if isinstance(expr, InExpr):
        return SqlIn(
            expr=_config_expr_to_sql(expr.expr, measure, config),
            values=[_config_expr_to_sql(value, measure, config) for value in expr.values],
            negated=expr.negated,
        )
    if isinstance(expr, BooleanExpr):
        rendered = [_config_expr_to_sql(arg, measure, config) for arg in expr.args]
        if not rendered:
            raise SemanticLayerError("INVALID_EXPRESSION_AST", "Boolean expressions require args")
        op = expr.op.lower()
        if op == "not":
            if len(rendered) != 1:
                raise SemanticLayerError(
                    "INVALID_EXPRESSION_AST",
                    f"Boolean 'not' expressions require exactly one arg, got {len(rendered)}",
                )
            # No unary-NOT node exists in the SQL AST; FALSE = (arg) has the
            # same three-valued truth table and forces parens around the arg.
            return SqlBinary(SqlLiteral(False), "=", rendered[0])
        current = rendered[0]
        for item in rendered[1:]:
            current = SqlBinary(current, op.upper(), item)
        return current
    if isinstance(expr, CallExpr):
        return SqlCall(
            expr.name,
            [_config_expr_to_sql(arg, measure, config) for arg in expr.args],
            distinct=expr.distinct,
        )
    if isinstance(expr, DateAddExpr):
        return dialect_for_warehouse(config.package.warehouse).date_add(
            expr.unit,
            _config_expr_to_sql(expr.value, measure, config),
            _config_expr_to_sql(expr.date, measure, config),
        )
    if isinstance(expr, CaseExpr):
        return SqlCase(
            whens=[
                SqlCaseWhen(
                    _config_expr_to_sql(item.when, measure, config),
                    _config_expr_to_sql(item.then, measure, config),
                )
                for item in expr.whens
            ],
            else_expr=_config_expr_to_sql(expr.else_expr, measure, config)
            if expr.else_expr is not None
            else None,
        )
    raise SemanticLayerError(
        "INVALID_EXPRESSION_AST",
        f"Unsupported measure expression kind '{expr_to_dict(expr)['kind']}'",
    )


def _expression_alias(expr: SemanticExpr, config: PackageConfig | None = None) -> str:
    if isinstance(expr, (MeasureRefExpr, AggregateExpr)):
        aggregation = expr.aggregation
        if not aggregation and config is not None:
            measure = _measure_index(config).get(expr.measure)
            aggregation = measure.default_aggregation if measure is not None else ""
        params_suffix = ""
        parameters = getattr(expr, "parameters", {}) or {}
        if parameters:
            parts = [
                f"{key}_{str(value).replace('.', '_')}" for key, value in sorted(parameters.items())
            ]
            params_suffix = "__" + "__".join(parts)
        return f"leaf__{expr.measure.replace('.', '_')}__{aggregation or 'default'}{params_suffix}"
    if isinstance(expr, ScopedAggregateExpr):
        if config is None:
            return f"leaf__scoped_{expr.measure.replace('.', '_')}"
        query = normalize_query(
            {"version": 2, "select": [{"expression": expr_to_dict(expr), "as": "__scoped"}]}
        )
        return _bind_scoped_aggregate(expr, config, query).alias
    if isinstance(expr, MetricRecipeRefExpr):
        return f"recipe__{expr.metric_recipe.replace('.', '_')}"
    if isinstance(expr, ConversionExpr):
        base = _expression_alias(expr.base, config).replace(".", "_")
        converted = _expression_alias(expr.converted, config).replace(".", "_")
        props = "__".join(prop.replace(".", "_") for prop in list(expr.constant_properties or []))
        prop_suffix = f"__{props}" if props else ""
        return f"conversion__{base}__to__{converted}__{expr.window_value}_{expr.window_unit}__{expr.matching_mode}{prop_suffix}"
    return f"{expr_to_dict(expr)['kind']}__expr"


def _bind_measure(
    expr: MeasureRefExpr, config: PackageConfig, query: NormalizedQuery
) -> BoundMeasure:
    measures = _measure_index(config)
    measure_id = expr.measure
    if measure_id not in measures:
        raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown measure '{measure_id}'")
    measure = measures[measure_id]
    aggregation = expr.aggregation or measure.default_aggregation
    if aggregation not in measure.allowed_aggregations:
        raise SemanticLayerError(
            "UNSUPPORTED_AGGREGATION",
            f"Aggregation '{aggregation}' is not allowed for '{measure_id}'",
            details={"measure": measure_id, "allowed": list(measure.allowed_aggregations)},
        )
    temporal_role = expr.temporal_role or query.temporal_role_overrides.get(measure_id, "")
    if (
        not temporal_role
        and query.time
        and query.time.temporal_role in measure.compatible_temporal_roles
    ):
        temporal_role = query.time.temporal_role
    if not temporal_role:
        temporal_role = _default_temporal_role(measure)
    if temporal_role and temporal_role not in measure.compatible_temporal_roles:
        raise SemanticLayerError(
            "INCOMPATIBLE_TEMPORAL_ROLE",
            f"Temporal role '{temporal_role}' is not compatible with '{measure_id}'",
            details={"measure": measure_id, "compatible": list(measure.compatible_temporal_roles)},
        )
    alias = _expression_alias(
        MeasureRefExpr(
            measure=measure_id,
            aggregation=aggregation,
            temporal_role=temporal_role,
            parameters=dict(expr.parameters or {}),
        ),
        config,
    )
    return BoundMeasure(
        measure_id=measure_id,
        aggregation=aggregation,
        alias=alias,
        temporal_role=temporal_role,
        aggregation_params=dict(expr.parameters or {}),
    )


def _scoped_predicate_expr_payload(predicate: dict[str, Any]) -> dict[str, Any]:
    raw = dict(predicate or {})
    input_expr = raw.get("input")
    if input_expr is None:
        metric_id = str(raw.get("metric", "")).strip()
        if metric_id:
            input_expr = {"kind": "metric", "metric": metric_id}
    if input_expr is None:
        # Accept `measure:` shorthand symmetric to `metric:` so authors
        # do not have to wrap a measure ref in `input: {measure: <id>}`
        # explicitly. With auto-publish gone, scoped predicates often
        # filter by a measure (e.g. session_starts), and the shorthand
        # keeps the YAML readable.
        measure_id = str(raw.get("measure", "")).strip()
        if measure_id:
            input_expr = {"measure": measure_id}
    if input_expr is None:
        raise SemanticLayerError(
            "PREDICATE_INPUT_REQUIRED",
            "scoped_aggregate predicates require 'metric', 'measure', or 'input'",
        )
    payload = {
        "kind": "metric_predicate",
        "input": input_expr,
        "entity": str(raw.get("entity", "")).strip(),
        "op": str(raw.get("op", "")).strip(),
        "value": raw.get("value"),
        "scope_mode": str(raw.get("scope_mode", "contextual") or "contextual"),
    }
    if raw.get("time_grain"):
        payload["time_grain"] = str(raw.get("time_grain", ""))
    if raw.get("time_alignment"):
        payload["time_alignment"] = str(raw.get("time_alignment", ""))
    if raw.get("window"):
        payload["window"] = dict(raw.get("window", {}) or {})
    return payload


def _scoped_aggregate_filter_spec(expr: ScopedAggregateExpr) -> dict[str, Any]:
    clauses: list[dict[str, Any]] = []
    for item in list(expr.where or []):
        field = str(item.get("field", "")).strip()
        if not field:
            raise SemanticLayerError(
                "INVALID_QUERY", "scoped_aggregate where clauses require a field"
            )
        clauses.append({"field": field, "op": str(item.get("op", "=")), "value": item.get("value")})
    for predicate in list(expr.predicates or []):
        clauses.append({"expression": _scoped_predicate_expr_payload(dict(predicate))})
    return {"all": clauses} if clauses else {}


def _validate_scoped_aggregate_anchor(expr: ScopedAggregateExpr, config: PackageConfig) -> None:
    """Validate that the anchor temporal role resolves to a per-entity
    scalar declared in the package. Raises ``INVALID_ANCHOR_ROLE``
    with details listing the available temporal roles when the
    anchor doesn't resolve.
    """
    anchor = dict(expr.anchor or {})
    role_id = str(anchor.get("temporal_role", "") or "")
    measure = _measure_index(config).get(expr.measure)
    measure_entity = measure.entity if measure else ""
    available_roles: list[str] = []
    for role in getattr(config, "temporal_roles", []) or []:
        rid = str(getattr(role, "id", "") or "")
        if not rid:
            continue
        available_roles.append(rid)
    if role_id and role_id not in available_roles:
        raise SemanticLayerError(
            "INVALID_ANCHOR_ROLE",
            (f"scoped_aggregate.anchor.temporal_role {role_id!r} is not defined in this package."),
            details={
                "anchor_role": role_id,
                "measure": expr.measure,
                "measure_entity": measure_entity,
                "available_temporal_roles": sorted(available_roles)[:30],
            },
        )


def _bind_scoped_aggregate(
    expr: ScopedAggregateExpr, config: PackageConfig, query: NormalizedQuery
) -> BoundMeasure:
    # Per-row event-anchored windows: validate the anchor at bind-time
    # before any measure-plan / SQL machinery runs. The IR shape is
    # parsed (see ``expressions.py``) and the schema is published, but
    # the SQL lowering still has to land. Until it does, block compile
    # with a structured error rather than silently aggregating events
    # outside the requested window — the silent-wrong-answer footgun
    # the reviewer flagged across rounds.
    if expr.anchor or expr.window:
        _validate_scoped_aggregate_anchor(expr, config)
        raise SemanticLayerError(
            "INVALID_ANCHOR_ROLE",
            (
                "Per-row event-anchored windows on scoped_aggregate are "
                "parsed and validated, but SQL lowering ships in the "
                "next round. The IR contract is stable; the compiler "
                "stub blocks execution to avoid silently aggregating "
                "events outside the requested window. Until the "
                "lowering lands, pre-author the windowed measure in the "
                "package (kind: scoped_aggregate inside a metric "
                "recipe) or run a two-step pipeline."
            ),
            details={
                "anchor": dict(expr.anchor or {}),
                "window": dict(expr.window or {}),
                "feature_status": "ir_contract_only",
                "tracking_note": (
                    "scoped_aggregate.anchor + scoped_aggregate.window "
                    "parse + validate today; SQL lowering deferred."
                ),
            },
        )
    bound = _bind_measure(
        MeasureRefExpr(
            measure=expr.measure,
            aggregation=expr.aggregation,
            temporal_role=expr.temporal_role,
            parameters=dict(expr.parameters or {}),
        ),
        config,
        query,
    )
    alias_payload = {
        "measure": bound.measure_id,
        "aggregation": bound.aggregation,
        "temporal_role": expr.temporal_role,
        "parameters": dict(bound.aggregation_params),
        "where": [dict(item) for item in list(expr.where or [])],
        "predicates": [dict(item) for item in list(expr.predicates or [])],
    }
    alias_hash = hashlib.sha1(_freeze_payload(alias_payload).encode("utf-8")).hexdigest()[:12]
    alias = f"leaf__scoped_{bound.measure_id.replace('.', '_')}__{alias_hash}"
    return BoundMeasure(
        measure_id=bound.measure_id,
        aggregation=bound.aggregation,
        alias=alias,
        temporal_role=bound.temporal_role,
        aggregation_params=dict(bound.aggregation_params),
        filter_spec=_scoped_aggregate_filter_spec(expr),
        window_spec={},
    )


def _resolve_filter_dimension(field: str, config: PackageConfig) -> str:
    dimensions = _dimension_index(config)
    if field in dimensions:
        return field
    matches = [
        row.id
        for row in config.dimensions
        if field in ({row.id, row.name, row.label, *row.aliases})
    ]
    if not matches:
        raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown filter field '{field}'")
    if len(matches) > 1:
        raise SemanticLayerError(
            "AMBIGUOUS_ALIAS",
            f"Filter field '{field}' is ambiguous",
            details={"field": field, "candidates": matches},
        )
    return matches[0]


def _bound_filter_clauses(bound: BoundMeasure, config: PackageConfig) -> list[dict[str, Any]]:
    filter_spec = dict(bound.filter_spec or {})
    clauses = list(filter_spec.get("all", []) or [])
    out: list[dict[str, Any]] = []
    for clause in clauses:
        if clause.get("expression") is not None:
            continue
        field = str(clause.get("field", "")).strip()
        if not field:
            continue
        out.append(
            {
                "field": _resolve_filter_dimension(field, config),
                "op": str(clause.get("op", "=")),
                "value": clause.get("value"),
            }
        )
    return out


def _parse_public_expr(payload: dict[str, Any]) -> SemanticExpr:
    return parse_semantic_expression(payload, context="query")


def _collect_measure_refs(
    expr: SemanticExpr, config: PackageConfig, query: NormalizedQuery, out: list[BoundMeasure]
) -> None:
    if isinstance(expr, MeasureRefExpr):
        out.append(_bind_measure(expr, config, query))
        return
    if isinstance(expr, AggregateExpr):
        if expr.window:
            # The schema accepts 'window' on aggregate nodes but no SQL
            # lowering consumes it — the windowed kinds (rolling /
            # prior_period / period_to_date) own that surface. Block at
            # bind time instead of silently returning the unwindowed
            # aggregate (the dropped-semantics footgun external agents
            # flagged: a "rolling 7-day" ask quietly became a plain SUM).
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST",
                (
                    "A 'window' on a plain aggregate expression is not lowered to "
                    "SQL. Wrap the aggregate in a windowed expression kind instead: "
                    "{kind: rolling|prior_period|period_to_date, input: {kind: "
                    "aggregate, measure: ...}, window: {unit, value}}."
                ),
                details={
                    "measure": expr.measure,
                    "window": dict(expr.window),
                    "supported_window_kinds": ["rolling", "prior_period", "period_to_date"],
                },
            )
        bound = _bind_measure(
            MeasureRefExpr(
                measure=expr.measure,
                aggregation=expr.aggregation,
                temporal_role=expr.temporal_role,
                parameters=dict(expr.parameters or {}),
            ),
            config,
            query,
        )
        out.append(
            BoundMeasure(
                measure_id=bound.measure_id,
                aggregation=bound.aggregation,
                alias=bound.alias,
                temporal_role=bound.temporal_role,
                aggregation_params=dict(bound.aggregation_params),
                filter_spec=dict(expr.filter),
                window_spec={},
            )
        )
        return
    if isinstance(expr, ScopedAggregateExpr):
        out.append(_bind_scoped_aggregate(expr, config, query))
        return
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        if recipe is None:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND", f"Unknown metric recipe '{expr.metric_recipe}'"
            )
        _collect_measure_refs(recipe.expression, config, query, out)
        return
    if isinstance(expr, ArithmeticExpr):
        _collect_measure_refs(expr.left, config, query, out)
        _collect_measure_refs(expr.right, config, query, out)
        return
    if isinstance(expr, RatioExpr):
        _collect_measure_refs(expr.numerator, config, query, out)
        _collect_measure_refs(expr.denominator, config, query, out)
        return
    if isinstance(expr, EntityValueExpr):
        _collect_measure_refs(expr.input, config, query, out)
        return
    if isinstance(expr, DistributionExpr):
        _collect_measure_refs(expr.over, config, query, out)
        return
    if isinstance(expr, ComparisonExpr):
        _collect_measure_refs(expr.left, config, query, out)
        _collect_measure_refs(expr.right, config, query, out)
        return
    if isinstance(expr, BooleanExpr):
        for arg in expr.args:
            _collect_measure_refs(arg, config, query, out)
        return
    if isinstance(expr, CallExpr):
        for arg in expr.args:
            _collect_measure_refs(arg, config, query, out)
        return
    if isinstance(expr, CaseExpr):
        for item in expr.whens:
            _collect_measure_refs(item.when, config, query, out)
            _collect_measure_refs(item.then, config, query, out)
        if expr.else_expr is not None:
            _collect_measure_refs(expr.else_expr, config, query, out)
        return
    if isinstance(
        expr, (CumulativeExpr, RollingExpr, PriorPeriodExpr, PeriodToDateExpr, OffsetWindowExpr)
    ):
        _collect_measure_refs(expr.input, config, query, out)
        return
    if isinstance(expr, MetricPredicateExpr):
        return
    if isinstance(expr, ConversionExpr):
        return
    if isinstance(expr, (LiteralExpr, ColumnRefExpr)):
        return
    raise SemanticLayerError(
        "INVALID_QUERY", f"Unsupported expression kind '{expr_to_dict(expr)['kind']}'"
    )


def _collect_conversion_exprs(
    expr: SemanticExpr, config: PackageConfig, out: list[ConversionExpr]
) -> None:
    if isinstance(expr, ConversionExpr):
        out.append(expr)
        return
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        if recipe is not None:
            _collect_conversion_exprs(recipe.expression, config, out)
        return
    if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
        _collect_conversion_exprs(expr.left, config, out)
        _collect_conversion_exprs(expr.right, config, out)
        return
    if isinstance(expr, RatioExpr):
        _collect_conversion_exprs(expr.numerator, config, out)
        _collect_conversion_exprs(expr.denominator, config, out)
        return
    if isinstance(expr, EntityValueExpr):
        _collect_conversion_exprs(expr.input, config, out)
        return
    if isinstance(expr, DistributionExpr):
        _collect_conversion_exprs(expr.over, config, out)
        return
    if isinstance(expr, BooleanExpr):
        for arg in expr.args:
            _collect_conversion_exprs(arg, config, out)
        return
    if isinstance(expr, CallExpr):
        for arg in expr.args:
            _collect_conversion_exprs(arg, config, out)
        return
    if isinstance(expr, CaseExpr):
        for item in expr.whens:
            _collect_conversion_exprs(item.when, config, out)
            _collect_conversion_exprs(item.then, config, out)
        if expr.else_expr is not None:
            _collect_conversion_exprs(expr.else_expr, config, out)
        return
    if isinstance(
        expr,
        (
            CumulativeExpr,
            RollingExpr,
            PriorPeriodExpr,
            PeriodToDateExpr,
            MetricPredicateExpr,
            OffsetWindowExpr,
        ),
    ):
        _collect_conversion_exprs(expr.input, config, out)


# ---------------------------------------------------------------------------
# aggregate_if (ConditionalAggregateExpr) — binding-time rewrite
# ---------------------------------------------------------------------------
#
# ``ConditionalAggregateExpr`` is a query-level shorthand for
# ``<AGG>(CASE WHEN cond THEN value END)``. The rest of the compiler is
# built around the assumption that every aggregation references a
# configured ``MeasureConfig`` looked up via ``measure_id``. To avoid
# touching every lookup site downstream, the binding phase rewrites each
# occurrence to a normal ``AggregateExpr`` backed by an anonymous
# synthetic measure (held on ``LogicalPlan.synthetic_measures``).
#
# Constraints surfaced through ``UNSUPPORTED_CONDITIONAL_AGGREGATE``:
#  - Every column ref inside ``condition`` / ``value`` must resolve to an
#    entity via ``entity`` or ``table`` (no surrounding-measure
#    fallback exists for an inline aggregate_if).
#  - All resolved columns must share a single entity. Cross-entity
#    aggregate_if is rejected.
#  - The aggregation must be a scalar aggregation that
#    ``_aggregation_expr`` already supports (count, sum, avg, min, max,
#    median, percentile). Window-only aggregations are rejected.


_AGGIF_SCALAR_AGGREGATIONS = frozenset(
    {"count", "count_distinct", "sum", "avg", "min", "max", "median", "percentile"}
)


def _resolve_column_entity(ref: ColumnRefExpr, config: PackageConfig) -> str:
    if ref.entity:
        return ref.entity
    if ref.table:
        entity_id = _table_to_entity(config).get(ref.table)
        if entity_id:
            return entity_id
        raise SemanticLayerError(
            "UNSUPPORTED_CONDITIONAL_AGGREGATE",
            (
                f"aggregate_if column references table '{ref.table}' that does not "
                "map to a known entity"
            ),
            details={"table": ref.table},
        )
    raise SemanticLayerError(
        "UNSUPPORTED_CONDITIONAL_AGGREGATE",
        (
            f"aggregate_if column '{ref.column}' must specify an 'entity' or 'table' "
            "(no surrounding measure to inherit from)"
        ),
        details={"column": ref.column},
    )


def _conditional_aggregate_entity(expr: ConditionalAggregateExpr, config: PackageConfig) -> str:
    refs: list[ColumnRefExpr] = []
    refs.extend(collect_column_refs(expr.condition))
    if expr.value is not None:
        refs.extend(collect_column_refs(expr.value))
    if not refs:
        raise SemanticLayerError(
            "UNSUPPORTED_CONDITIONAL_AGGREGATE",
            "aggregate_if expressions must reference at least one column",
        )
    entities = {_resolve_column_entity(ref, config) for ref in refs}
    if len(entities) > 1:
        raise SemanticLayerError(
            "UNSUPPORTED_CONDITIONAL_AGGREGATE",
            (
                f"aggregate_if references columns from multiple entities {sorted(entities)} "
                "— inline conditional aggregates must resolve to a single entity"
            ),
            details={"entities": sorted(entities)},
        )
    return next(iter(entities))


def _synthetic_conditional_measure(
    expr: ConditionalAggregateExpr, config: PackageConfig
) -> tuple[str, MeasureConfig]:
    aggregation = (expr.aggregation or "").lower()
    if aggregation not in _AGGIF_SCALAR_AGGREGATIONS:
        raise SemanticLayerError(
            "UNSUPPORTED_CONDITIONAL_AGGREGATE",
            (
                f"aggregate_if aggregation '{expr.aggregation}' is not supported. "
                f"Allowed: {sorted(_AGGIF_SCALAR_AGGREGATIONS)}"
            ),
            details={"aggregation": expr.aggregation},
        )
    entity_id = _conditional_aggregate_entity(expr, config)
    entities = _entity_index(config)
    entity = entities[entity_id]

    # Build the column-level expression: CASE WHEN cond THEN value END.
    # For COUNT_IF (value omitted) the body is literal 1 so COUNT()
    # only counts matching rows. Note: we do NOT add an ELSE branch —
    # the implicit NULL behaviour is what every aggregation needs to
    # ignore non-matching rows correctly.
    body_value: SemanticExpr = expr.value if expr.value is not None else LiteralExpr(value=1)
    expr_for_measure = CaseExpr(
        whens=[CaseWhenExpr(when=expr.condition, then=body_value)],
        else_expr=None,
    )

    payload = json.dumps(
        {
            "aggregation": aggregation,
            "entity": entity_id,
            "expr": expr_to_dict(expr_for_measure),
        },
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    measure_id = f"measure.__aggif__.{digest}"

    measure = MeasureConfig(
        id=measure_id,
        entity=entity_id,
        subject_entity=entity_id,
        aggregation_entity=entity_id,
        row_grain=[],
        expr=expr_for_measure,
        default_aggregation=aggregation,
        allowed_aggregations=[aggregation],
        source_relation=entity.table,
        measure_class="additive",
        value_type="number",
        name=f"aggregate_if[{aggregation}]",
        label=f"aggregate_if({aggregation}, …)",
        description=("Synthetic measure produced by the aggregate_if binding-time rewrite."),
        meta={"synthetic": True, "source": "aggregate_if"},
    )
    return measure_id, measure


def _rewrite_conditional_aggregates(
    expr: SemanticExpr, config: PackageConfig, synthetic: dict[str, MeasureConfig]
) -> SemanticExpr:
    """Walk ``expr`` replacing every ConditionalAggregateExpr with an
    ``AggregateExpr`` referencing a freshly synthesised measure. Mutates
    ``synthetic`` in place. Returns the (possibly new) root node.
    """
    if isinstance(expr, ConditionalAggregateExpr):
        measure_id, measure_config = _synthetic_conditional_measure(expr, config)
        synthetic.setdefault(measure_id, measure_config)
        return AggregateExpr(
            measure=measure_id,
            aggregation=measure_config.default_aggregation,
            temporal_role="",
            parameters={},
            filter={},
            window={},
        )
    if isinstance(expr, ArithmeticExpr):
        return ArithmeticExpr(
            op=expr.op,
            left=_rewrite_conditional_aggregates(expr.left, config, synthetic),
            right=_rewrite_conditional_aggregates(expr.right, config, synthetic),
            null_behavior=expr.null_behavior,
        )
    if isinstance(expr, ComparisonExpr):
        return ComparisonExpr(
            op=expr.op,
            left=_rewrite_conditional_aggregates(expr.left, config, synthetic),
            right=_rewrite_conditional_aggregates(expr.right, config, synthetic),
        )
    if isinstance(expr, BooleanExpr):
        return BooleanExpr(
            op=expr.op,
            args=[_rewrite_conditional_aggregates(arg, config, synthetic) for arg in expr.args],
        )
    if isinstance(expr, CallExpr):
        return CallExpr(
            name=expr.name,
            args=[_rewrite_conditional_aggregates(arg, config, synthetic) for arg in expr.args],
            distinct=expr.distinct,
        )
    if isinstance(expr, RatioExpr):
        return RatioExpr(
            numerator=_rewrite_conditional_aggregates(expr.numerator, config, synthetic),
            denominator=_rewrite_conditional_aggregates(expr.denominator, config, synthetic),
            null_behavior=expr.null_behavior,
        )
    if isinstance(expr, CaseExpr):
        return CaseExpr(
            whens=[
                CaseWhenExpr(
                    when=_rewrite_conditional_aggregates(item.when, config, synthetic),
                    then=_rewrite_conditional_aggregates(item.then, config, synthetic),
                )
                for item in expr.whens
            ],
            else_expr=(
                _rewrite_conditional_aggregates(expr.else_expr, config, synthetic)
                if expr.else_expr is not None
                else None
            ),
        )
    # Nodes that wrap an aggregation (CumulativeExpr etc.) recurse into
    # their ``input``. Other node types are leaves or carry semantics
    # incompatible with aggregate_if and pass through unchanged.
    if isinstance(
        expr,
        (
            CumulativeExpr,
            RollingExpr,
            PriorPeriodExpr,
            PeriodToDateExpr,
            OffsetWindowExpr,
            MetricPredicateExpr,
        ),
    ):
        # These wrappers may not legitimately contain a top-level
        # aggregate_if as their direct input (the inner expression must
        # itself be an aggregation already). We still recurse so a
        # rewrite caught deeper in the tree (e.g. inside a CaseExpr
        # used as ``input``) is handled correctly.
        rewritten_input = _rewrite_conditional_aggregates(expr.input, config, synthetic)
        if rewritten_input is expr.input:
            return expr
        # Re-emit via the public dict shape to avoid forking copy logic
        # for each wrapper type.
        payload = expr_to_dict(expr)
        payload["input"] = expr_to_dict(rewritten_input)
        return parse_semantic_expression(payload, context="rewrite")
    return expr


def lift_conditional_aggregates(
    query: NormalizedQuery, config: PackageConfig
) -> tuple[NormalizedQuery, dict[str, MeasureConfig]]:
    """Walk every expression in ``query`` and replace each
    ``ConditionalAggregateExpr`` with an ``AggregateExpr`` keyed to a
    freshly synthesised ``MeasureConfig``. Returns the rewritten query
    plus the synthetic-measure dict to attach to the ``LogicalPlan``.
    """
    from dataclasses import replace as _replace

    from ..ast import MetricFilter, QuerySelect

    synthetic: dict[str, MeasureConfig] = {}

    def _maybe_rewrite(expr: SemanticExpr | None) -> SemanticExpr | None:
        if expr is None:
            return None
        return _rewrite_conditional_aggregates(expr, config, synthetic)

    new_select = [
        QuerySelect(expression=_maybe_rewrite(item.expression), as_=item.as_)
        for item in query.select
    ]
    new_metric_filters = [
        MetricFilter(
            expression=_maybe_rewrite(item.expression),
            op=item.op,
            value=item.value,
        )
        for item in query.metric_filters
    ]
    if not synthetic:
        return query, {}
    return _replace(query, select=new_select, metric_filters=new_metric_filters), synthetic
