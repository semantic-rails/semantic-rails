from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
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
    resolve_filter_dimension,
    resolve_measure_temporal_role,
    validate_boolean_argument_count,
)
from ..ir import BoundMeasure
from ..schema import MeasureConfig, PackageConfig
from ..sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCase,
    SqlCaseWhen,
    SqlIdentifier,
    SqlIn,
    SqlLiteral,
    build_comparison_condition,
    build_negation,
)
from .dependencies import binding_cut, measure_cut_owners, measure_objects
from .indexes import (
    _dimension_index,
    _entity_index,
    _measure_index,
    _recipe_index,
    _relationship_index,
    _resolve_table_entity,
)
from .paths import _column_ref, _reaches_at_most_one


def _freeze_payload(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def is_conditional_case(expr: Any) -> bool:
    """Whether an aggregate of ``expr``, a CASE in SQL or in a measure's config, reads only the
    rows one of its conditions keeps. Only no ELSE or ELSE NULL can exclude other rows: even
    ELSE 0 contributes a known value when every matching body is NULL."""
    if not isinstance(expr, SqlCase | CaseExpr):
        return False
    other = expr.else_expr
    return other is None or (isinstance(other, SqlLiteral | LiteralExpr) and other.value is None)


def _row_marker(expr: Any) -> Any:
    """1 on each row an aggregate of ``expr`` reads, NULL on the rest: a conditional CASE reads
    only the rows one of its conditions keeps (``aggregate_if``), any other every row."""
    if not (isinstance(expr, SqlCase) and is_conditional_case(expr)):
        return SqlLiteral(1)
    return SqlCase([SqlCaseWhen(item.condition, SqlLiteral(1)) for item in expr.whens])


_earlier_settlement: ContextVar[bool] = ContextVar("earlier_settlement", default=False)


@contextmanager
def earlier_settlement(enabled: bool = True) -> Iterator[None]:
    """While ``enabled``, settle as before unknown amounts stayed NULL: the guard
    (``empty_groups``) reads a NULL sum as 0 wherever its measure has data in scope, whether
    its group has no rows or only rows of unknown amounts, so no leaf counts its rows;
    a rollup may answer a CASE measure. A query with a distribution branch turns it on for
    every branch (``_lower_query_to_sql``), and so
    does a metric predicate's source over several measures under a threshold that 0 passes
    (``_predicate_ctes_and_join``). It is off by default, and a nested block never turns it
    off."""
    token = _earlier_settlement.set(enabled or _earlier_settlement.get())
    try:
        yield
    finally:
        _earlier_settlement.reset(token)


def earlier_settlement_applies() -> bool:
    return _earlier_settlement.get()


def _maybe_conditional_aggregate(expr: Any, aggregation: str, dialect: SqlDialect) -> Any | None:
    """If ``expr`` is the canonical ``CASE WHEN cond THEN body END``
    shape that ``aggregate_if`` produces (one when, no else — or an
    ELSE NULL), delegate to ``dialect.conditional_aggregate`` so
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
    """
    if not isinstance(expr, SqlCase) or len(expr.whens) != 1:
        return None
    if not is_conditional_case(expr):
        return None
    agg = aggregation.lower()
    condition, body = expr.whens[0].condition, expr.whens[0].result
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
        entity_id = _resolve_table_entity(config, ref.table, owner=measure.entity)
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


def measure_column_ref(
    ref: ColumnRefExpr, measure: MeasureConfig, config: PackageConfig
) -> SqlIdentifier:
    """The source column shared by SQL lowering and column-aggregate constraints."""
    entity_id = _resolve_expr_entity(ref, measure, config)
    entity = _entity_index(config)[entity_id]
    # A measure's own fact relation can differ from its entity's relation.
    source = measure.source_relation if entity_id == measure.entity and not ref.table else ""
    return _column_ref(source or entity.table, ref.column)


def _config_expr_to_sql(expr: SemanticExpr, measure: MeasureConfig, config: PackageConfig) -> Any:
    if measure.lookup_from:
        # Its expr is the key to its parent; only the parent_lookup leaf reads its value.
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED",
            f"Lookup measure '{measure.id}' is read only through its parent_lookup leaf.",
            details={"measure_id": measure.id, "unsupported_construct": "parent_lookup"},
        )
    with measure_objects(measure.id):
        return _config_expr_to_sql_inner(expr, measure, config)


def _config_expr_to_sql_inner(
    expr: SemanticExpr, measure: MeasureConfig, config: PackageConfig
) -> Any:
    if isinstance(expr, ColumnRefExpr):
        entity_id = _resolve_expr_entity(expr, measure, config)
        if is_conditional_aggregate(measure):
            bind_conditional_aggregate_column(measure, entity_id, expr.column, config)
        return measure_column_ref(expr, measure, config)
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
        return build_comparison_condition(
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
        validate_boolean_argument_count(expr.op, len(expr.args))
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
            return build_negation(rendered[0])
        current = rendered[0]
        for item in rendered[1:]:
            current = SqlBinary(current, op.upper(), item)
        return current
    if isinstance(expr, CallExpr):
        return dialect_for_warehouse(config.package.warehouse).scalar_call(
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
        # Only the synthetic wrapper selects aggregate rows. Authored CASE
        # expressions inside its condition or value keep their own ELSE paths.
        conditional_wrapper = expr is measure.expr and measure.meta.get("source") == "aggregate_if"
        whens = []
        for item in expr.whens:
            with (
                measure_cut_owners(measure.id) if conditional_wrapper else nullcontext(),
                binding_cut() if conditional_wrapper else nullcontext(),
            ):
                condition = _config_expr_to_sql(item.when, measure, config)
            whens.append(SqlCaseWhen(condition, _config_expr_to_sql(item.then, measure, config)))
        return SqlCase(
            whens=whens,
            else_expr=_config_expr_to_sql(expr.else_expr, measure, config)
            if expr.else_expr is not None
            else None,
        )
    raise SemanticLayerError(
        "INVALID_EXPRESSION_AST",
        f"Unsupported measure expression kind '{expr_to_dict(expr)['kind']}'",
    )


def _scope_key(expr: MeasureRefExpr | AggregateExpr) -> str:
    """A leaf alias suffix for the expression's own temporal_role and filter, when set.

    Without it, aggregates of one measure that differ only by filter or by clock
    shared one column, and the first one selected answered for both.
    """
    scope = {"temporal_role": expr.temporal_role, "filter": getattr(expr, "filter", {}) or {}}
    if not any(scope.values()):
        return ""
    return "__" + hashlib.sha1(_freeze_payload(scope).encode("utf-8")).hexdigest()[:12]


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
        return f"leaf__{expr.measure.replace('.', '_')}__{aggregation or 'default'}{params_suffix}{_scope_key(expr)}"
    if isinstance(expr, ScopedAggregateExpr):
        if config is None:
            return f"leaf__scoped_{expr.measure.replace('.', '_')}"
        query = normalize_query(
            {"version": 1, "select": [{"expression": expr_to_dict(expr), "as": "__scoped"}]}
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
    expr: MeasureRefExpr,
    config: PackageConfig,
    query: NormalizedQuery,
    *,
    conversion_operand: bool = False,
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
            details={
                "measure": measure_id,
                "aggregation": aggregation,
                "allowed": list(measure.allowed_aggregations),
                "default_aggregation": measure.default_aggregation,
            },
        )
    temporal_role = resolve_measure_temporal_role(
        measure,
        expr.temporal_role,
        query.temporal_role_overrides,
        query.time.temporal_role if query.time else "",
    )
    if temporal_role and temporal_role not in measure.compatible_temporal_roles:
        details: dict[str, Any] = {
            "measure": measure_id,
            "compatible": list(measure.compatible_temporal_roles),
        }
        if not measure.compatible_temporal_roles:
            # No clock at all: name the role asked for, so the hint says to declare one.
            details["requested"] = temporal_role
        raise SemanticLayerError(
            "INCOMPATIBLE_TEMPORAL_ROLE",
            f"Temporal role '{temporal_role}' is not compatible with '{measure_id}'",
            details=details,
        )
    query_role = query.time.temporal_role if query.time else ""
    if query_role and not temporal_role and not conversion_operand:
        if measure_id.startswith("measure.__aggif__."):
            # An aggregate_if has no model or measure of its own to declare a clock on.
            raise SemanticLayerError(
                "INCOMPATIBLE_TEMPORAL_ROLE",
                f"aggregate_if can't be used with time ('{query_role}'); declare a measure "
                "with `times:` and aggregate that instead, or drop `time` from the query.",
                details={"requested": query_role, "compatible": [], "source": "aggregate_if"},
            )
        # No clock to bucket by: the plan has no role to read, so refuse here, in the one
        # place every measure is bound, instead of failing later on a missing role.
        raise SemanticLayerError(
            "INCOMPATIBLE_TEMPORAL_ROLE",
            f"'{measure_id}' has no time role, so it can't be placed on '{query_role}'. Mark "
            f"a time on its model `default: true`, or list `times:` on the measure; or drop "
            f"`time` from the query.",
            details={"measure": measure_id, "requested": query_role, "compatible": []},
        )
    compatible = list(measure.compatible_temporal_roles)
    # Conversion operands keep their own rules (_validate_conversion_temporal_bindings).
    named = (
        conversion_operand or expr.temporal_role or query.temporal_role_overrides.get(measure_id)
    )
    if query_role and query_role not in compatible and len(compatible) > 1 and not named:
        # A measure the query's clock doesn't fit is timed by its own. With several
        # clocks, none of them the query's, the first one would be a guess.
        raise SemanticLayerError(
            "INCOMPATIBLE_TEMPORAL_ROLE",
            f"'{measure_id}' isn't timed by '{query_role}' and has several clocks of its own "
            f"(see `compatible`). Choose one with temporal_role_overrides "
            f"{{'{measure_id}': <clock>}}, or query a clock the measure has.",
            details={"measure": measure_id, "requested": query_role, "compatible": compatible},
        )
    alias = _expression_alias(
        MeasureRefExpr(
            measure=measure_id,
            aggregation=aggregation,
            temporal_role=expr.temporal_role,
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
                "events outside the requested window. A metric recipe "
                "that authors the same anchor and window is refused the "
                "same way. Until the lowering lands, expose the offset "
                "from the anchor as a column (for example days since "
                "the first order) and filter a measure on it."
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
    dimension_id = resolve_filter_dimension(field, config)
    _dimension_index(config)[dimension_id]
    return dimension_id


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
        with binding_cut():
            dimension_id = _resolve_filter_dimension(field, config)
        out.append(
            {
                "field": dimension_id,
                "op": str(clause.get("op", "=")),
                "value": clause.get("value"),
            }
        )
    return out


def _parse_public_expr(payload: dict[str, Any]) -> SemanticExpr:
    return parse_semantic_expression(payload, context="query")


def _bound_metric_predicates(bound: BoundMeasure) -> list[MetricPredicateExpr]:
    filter_spec = dict(bound.filter_spec or {})
    clauses = list(filter_spec.get("all", []) or [])
    predicates: list[MetricPredicateExpr] = []
    for clause in clauses:
        raw_expr = clause.get("expression")
        if raw_expr is None:
            continue
        expr = _parse_public_expr(dict(raw_expr))
        if not isinstance(expr, MetricPredicateExpr):
            raise SemanticLayerError(
                "INVALID_METRIC_PREDICATE",
                "Only metric_predicate expressions are supported in aggregate filters",
            )
        predicates.append(expr)
    return predicates


def _measure_count_distinct_key_columns(measure: MeasureConfig, config: PackageConfig) -> list[str]:
    if measure.measure_class not in {"event_count", "distinct_population"}:
        return []
    if not isinstance(measure.expr, ColumnRefExpr):
        return []
    if measure.expr.entity or measure.expr.table:
        return []
    entity = _entity_index(config).get(measure.entity)
    if entity is None:
        return []
    key_cols = list(entity.key or [entity.primary_key])
    return key_cols if measure.expr.column in set(key_cols) else []


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
                alias=_expression_alias(expr, config),
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
# An aggregate_if aggregates at the grain of its value's entity (the base);
# every other column it reads must be reachable from the base by one
# unambiguous chain of declared many-to-one hops.
# ``check_conditional_aggregate_path`` holds that rule, and every measure
# leaf that plans the synthetic measure's joins runs it on the path a
# ``where`` filter on that entity would take.
#
# Constraints surfaced through ``UNSUPPORTED_CONDITIONAL_AGGREGATE``:
#  - Every column ref inside ``condition`` / ``value`` must resolve to an
#    entity via ``entity`` or ``table`` (no surrounding-measure
#    fallback exists for an inline aggregate_if).
#  - The ``value`` columns share one entity, the base. Without a value
#    column, the condition's columns must share one entity: which rows a
#    count counts is otherwise ambiguous.
#  - Each other entity the condition reads is reached from the base over
#    N:1 or 1:1 hops only: no one-to-many, many-to-many or bridge hop, no
#    hop valid over time, and one route (pin ambiguous routes and roles
#    with a path preference).
#  - A value row with no match on the path never satisfies the condition:
#    for each such entity, a top-level AND term compares a bare column of
#    it (=, !=, <, <=, >, >=, IN, NOT IN, IS NOT NULL). A condition such a
#    row could satisfy (IS NULL, an OR with the base's own column) is
#    refused, so whether the lookup joins LEFT or INNER changes no value.
#  - The aggregation must be a scalar aggregation that
#    ``_aggregation_expr`` already supports (count, sum, avg, min, max,
#    median, percentile). Window-only aggregations are rejected.
#
# Each column it reads, including on its own entity, binds the dimensions
# over that column, as a ``where`` filter binds its dimension, so object
# policies on them refuse it (``bind_conditional_aggregate_column``).


_AGGIF_SCALAR_AGGREGATIONS = frozenset(
    {"count", "count_distinct", "sum", "avg", "min", "max", "median", "percentile"}
)


def _resolve_column_entity(ref: ColumnRefExpr, config: PackageConfig) -> str:
    if ref.entity:
        return ref.entity
    if ref.table:
        entity_id = _resolve_table_entity(config, ref.table)
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
    """The base entity: the one entity of the value's columns, else of the condition's."""
    value_refs = collect_column_refs(expr.value) if expr.value is not None else []
    condition_refs = collect_column_refs(expr.condition)
    if not value_refs and not condition_refs:
        raise SemanticLayerError(
            "UNSUPPORTED_CONDITIONAL_AGGREGATE",
            "aggregate_if expressions must reference at least one column",
        )
    value_entities = {_resolve_column_entity(ref, config) for ref in value_refs}
    condition_entities = {_resolve_column_entity(ref, config) for ref in condition_refs}
    if len(value_entities) > 1:
        raise SemanticLayerError(
            "UNSUPPORTED_CONDITIONAL_AGGREGATE",
            (
                f"aggregate_if value reads columns from several entities {sorted(value_entities)}; "
                "it aggregates the rows of one entity, so its value must come from that entity"
            ),
            details={
                "entities": sorted(value_entities),
                "reason": "value_spans_entities",
                "hint": (
                    "Take the value from one entity; the condition may read the entities it "
                    "reaches over many-to-one hops."
                ),
            },
        )
    if value_entities:
        return next(iter(value_entities))
    if len(condition_entities) > 1:
        raise SemanticLayerError(
            "UNSUPPORTED_CONDITIONAL_AGGREGATE",
            (
                f"aggregate_if({expr.aggregation}) has no value column and its condition reads "
                f"several entities {sorted(condition_entities)}, so which entity's rows it "
                "aggregates is ambiguous"
            ),
            details={
                "entities": sorted(condition_entities),
                "reason": "ambiguous_grain",
                "hint": (
                    "Add a 'value' column of the entity whose rows to aggregate, e.g. its key "
                    "to count its rows."
                ),
            },
        )
    return next(iter(condition_entities))


# A NULL operand makes these comparisons NULL, so a row with no match fails them.
_NULL_REJECTING_OPS = frozenset({"=", "!=", "<>", "<", "<=", ">", ">=", "IN", "NOT IN"})


def _and_terms(expr: SemanticExpr) -> list[SemanticExpr]:
    if isinstance(expr, BooleanExpr) and expr.op.lower() == "and":
        return [term for arg in expr.args for term in _and_terms(arg)]
    return [expr]


def _null_rejected_entities(term: SemanticExpr, config: PackageConfig) -> set[str]:
    """The entities one of whose bare columns, read as NULL, makes ``term`` not true."""
    if isinstance(term, InExpr):
        if isinstance(term.expr, ColumnRefExpr) and not any(
            isinstance(value, LiteralExpr) and value.value is None for value in term.values
        ):
            return {_resolve_column_entity(term.expr, config)}
        return set()
    if not isinstance(term, ComparisonExpr):
        return set()
    op = " ".join(term.op.split()).upper()
    rejected = set()
    for column, other in ((term.left, term.right), (term.right, term.left)):
        if not isinstance(column, ColumnRefExpr):
            continue
        # `= null` reads as IS NULL, which a row with no match satisfies.
        if isinstance(other, LiteralExpr) and other.value is None:
            rejects = op in {"!=", "<>", "IS NOT"}
        else:
            rejects = op in _NULL_REJECTING_OPS
        if rejects:
            rejected.add(_resolve_column_entity(column, config))
    return rejected


def _require_null_rejecting_condition(
    expr: ConditionalAggregateExpr, base: str, config: PackageConfig
) -> None:
    """Refuse a condition that a value row with no match on its path could satisfy.

    Each entity other than the base that the condition reads needs a top-level AND term
    comparing a bare column of it, so that such a row fails the condition whether its lookup
    joins LEFT or INNER, and the join kind never changes a value.
    """
    terms = _and_terms(expr.condition)
    rejected = {entity for term in terms for entity in _null_rejected_entities(term, config)}
    read = {_resolve_column_entity(ref, config) for ref in collect_column_refs(expr.condition)}
    accepting = sorted(read - rejected - {base})
    if accepting:
        entity = accepting[0]
        raise _conditional_path_refusal(
            base,
            entity,
            "a value row with no match there could satisfy the condition",
            "null_accepting_condition",
            (
                f"Compare a column of '{entity}' with =, !=, <, <=, >, >=, IN, NOT IN or "
                "IS NOT NULL in a top-level AND term of the condition, or use a where filter "
                "on its dimension with the plain measure."
            ),
        )


def is_conditional_aggregate(measure: MeasureConfig) -> bool:
    return measure.meta.get("source") == "aggregate_if"


# Policy kinds that refuse a query by the ids of the objects it reads.
_OBJECT_POLICY_KINDS = frozenset({"object_access", "object_visibility"})


def bind_conditional_aggregate_column(
    measure: MeasureConfig, entity_id: str, column: str, config: PackageConfig
) -> None:
    """Bind a column an aggregate_if reads as a where filter binds its dimension.

    Every lowering of the measure's expression reads its columns here, so each dimension over
    the column becomes a dependency that object policies see before SQL is rendered. A column
    of another entity that no dimension declares cannot be named by a policy, so it is refused
    whenever the package declares an object policy. An own-entity column with no dimension
    remains allowed, as a measure's value column usually has none.
    """
    joined = _measure_required_entities(measure, config) - {measure.entity}
    dimensions = [
        row.id
        for row in config.dimensions
        if row.entity == entity_id and row.column.casefold() == column.casefold()
    ]
    if (
        not dimensions
        and entity_id in joined
        and any(policy.kind in _OBJECT_POLICY_KINDS for policy in config.semantic_policies)
    ):
        raise SemanticLayerError(
            "POLICY_DENIED",
            (
                f"aggregate_if over '{measure.entity}' reads column '{column}' of "
                f"'{entity_id}', which no dimension declares, so object policies "
                "cannot govern it"
            ),
            details={
                "reason": "column_without_dimension",
                "entity": entity_id,
                "column": column,
                "hint": (
                    "Declare a dimension on the column, or use a where filter on a "
                    "dimension of that entity with the plain measure."
                ),
            },
        )
    index = _dimension_index(config)
    for dimension_id in dimensions:
        index.get(dimension_id)


def _conditional_path_refusal(
    base: str, target: str, message: str, reason: str, hint: str, **details: Any
) -> SemanticLayerError:
    return SemanticLayerError(
        "UNSUPPORTED_CONDITIONAL_AGGREGATE",
        f"aggregate_if over '{base}' reads '{target}': {message}",
        details={
            "base_entity": base,
            "entity": target,
            "reason": reason,
            "hint": hint,
            **details,
        },
    )


def conditional_aggregate_route_refusal(
    measure: MeasureConfig, target: str, exc: SemanticLayerError
) -> SemanticLayerError:
    """The aggregate_if refusal for an entity its base reaches by no route, or by two (with
    the route refusal's ``clarification``)."""
    return _conditional_path_refusal(
        measure.entity,
        target,
        str(exc),
        exc.code.lower(),
        str(exc.details.get("hint", ""))
        or "Declare a many-to-one relationship from the value's entity to this entity.",
        **(
            {"clarification": exc.details["clarification"]}
            if "clarification" in exc.details
            else {}
        ),
    )


def check_conditional_aggregate_path(
    measure: MeasureConfig, target: str, path: list[str], config: PackageConfig
) -> None:
    """Refuse an aggregate_if unless every hop of its path to ``target`` reaches at most one row.

    The path is the one a ``where`` filter on that entity's dimension takes (path
    preferences and role rules included), so no value row is counted twice.
    """
    relationships = _relationship_index(config)
    current = measure.entity
    for rel_id in path:
        rel = relationships[rel_id]
        forward = current == rel.source_entity
        following = rel.target_entity if forward else rel.source_entity
        if rel.temporal_validity or not _reaches_at_most_one(rel, current):
            cardinality = rel.cardinality if forward else ":".join(rel.cardinality.split(":")[::-1])
            why, hint = (
                (
                    "is valid over time",
                    "An aggregate_if has no time to pick one version by; read an entity "
                    "that holds one row per key instead.",
                )
                if rel.temporal_validity
                else (
                    f"is {cardinality}: it can reach many rows",
                    "A condition may read only the entities its value's entity reaches "
                    "over many-to-one hops. To keep rows by what their related rows hold, "
                    "use a metric_predicate on the value's entity instead.",
                )
            )
            raise _conditional_path_refusal(
                measure.entity,
                target,
                f"the hop '{rel.id}' from '{current}' to '{following}' {why}, so one value "
                "row could be aggregated more than once",
                "fanout_hop",
                hint,
                path=list(path),
                relationship=rel.id,
                from_entity=current,
                to_entity=following,
                cardinality=cardinality,
            )
        current = following


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
    _require_null_rejecting_condition(expr, entity_id, config)
    entities = _entity_index(config)
    entity = entities[entity_id]
    # It aggregates the rows of its entity's table, whose grain the measures of that table
    # declare. A grain other than the entity's key wins, so a rewrite that relies on one row
    # per key refuses this measure as it refuses theirs. With no such measure it is unknown.
    key = sorted(entity.key or [entity.primary_key])
    grains = sorted(
        list(row.row_grain)
        for row in config.measures
        if row.entity == entity_id and row.source_relation in {"", entity.table} and row.row_grain
    )
    row_grain = next(
        (grain for grain in grains if sorted(grain) != key), grains[0] if grains else []
    )

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
        row_grain=row_grain,
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
