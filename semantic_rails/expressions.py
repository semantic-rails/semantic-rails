"""Semantic expression AST and the package-expression mini-parser.

Defines the frozen dataclass nodes (``MeasureRefExpr``, ``AggregateExpr``,
``ArithmeticExpr``, ``MetricPredicateExpr``, ``CumulativeExpr``,
``PeriodToDateExpr``, …) that compose every metric-recipe and
measure-expression body. :func:`parse_config_expression` and
:func:`parse_semantic_expression` translate the YAML string form into
these nodes; the compiler consumes them.
"""

from __future__ import annotations

import ast as pyast
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .errors import SemanticLayerError


@dataclass(frozen=True)
class MeasureRefExpr:
    measure: str
    aggregation: str = ""
    temporal_role: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AggregateExpr:
    measure: str
    aggregation: str = ""
    temporal_role: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    filter: dict[str, Any] = field(default_factory=dict)
    window: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricRecipeRefExpr:
    metric_recipe: str


@dataclass(frozen=True)
class ColumnRefExpr:
    column: str
    entity: str = ""
    table: str = ""


@dataclass(frozen=True)
class LiteralExpr:
    value: Any


@dataclass(frozen=True)
class ArithmeticExpr:
    op: str
    left: SemanticExpr
    right: SemanticExpr
    null_behavior: str = ""


@dataclass(frozen=True)
class ComparisonExpr:
    op: str
    left: SemanticExpr
    right: SemanticExpr


@dataclass(frozen=True)
class BooleanExpr:
    op: str
    args: list[SemanticExpr] = field(default_factory=list)


@dataclass(frozen=True)
class CallExpr:
    name: str
    args: list[SemanticExpr] = field(default_factory=list)
    distinct: bool = False


@dataclass(frozen=True)
class DateAddExpr:
    unit: str
    value: SemanticExpr
    date: SemanticExpr


@dataclass(frozen=True)
class InExpr:
    expr: SemanticExpr
    values: list[SemanticExpr] = field(default_factory=list)
    negated: bool = False


@dataclass(frozen=True)
class CaseWhenExpr:
    when: SemanticExpr
    then: SemanticExpr


@dataclass(frozen=True)
class CaseExpr:
    whens: list[CaseWhenExpr] = field(default_factory=list)
    else_expr: SemanticExpr | None = None


@dataclass(frozen=True)
class CumulativeExpr:
    input: SemanticExpr
    partition_by: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RollingExpr:
    input: SemanticExpr
    unit: str
    value: int
    partition_by: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PriorPeriodExpr:
    input: SemanticExpr
    unit: str
    value: int = 1


@dataclass(frozen=True)
class PeriodToDateExpr:
    input: SemanticExpr
    period: str
    partition_by: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OffsetWindowExpr:
    input: SemanticExpr
    kind: str
    aggregate: str
    frame: str = ""
    extra_partition: str = ""
    period: str = ""
    unit: str = ""
    value: int = 0
    partition_by: list[str] = field(default_factory=list)
    window_scope: str = ""


@dataclass(frozen=True)
class MetricPredicateExpr:
    input: SemanticExpr
    entity: str
    op: str
    value: Any
    scope_mode: str = ""
    time_grain: str = ""
    time_alignment: str = ""
    window_unit: str = ""
    window_value: int = 0


@dataclass(frozen=True)
class ConditionalAggregateExpr:
    """An aggregation over a single CASE WHEN expression.

    ``aggregate_if(agg, condition[, value])`` — emits ``<AGG>(CASE WHEN
    condition THEN value END)`` (or ``COUNT_IF(condition)`` /
    ``SUM_IF(value, condition)`` natively on Snowflake). When the
    aggregation is ``count``, ``value`` may be omitted; the node then
    represents ``COUNT_IF(condition)``.

    Unlike ``AggregateExpr`` / ``ScopedAggregateExpr``, this node does
    not reference a configured measure id. The condition and value are
    arbitrary semantic expressions over columns. The compiler rewrites
    each occurrence into a synthetic ``AggregateExpr`` backed by an
    anonymous ``MeasureConfig`` so the rest of the lowering pipeline
    sees a normal measure reference. See
    ``compiler_parts/bind.py:_lift_conditional_aggregates`` for the
    rewrite.
    """

    aggregation: str
    condition: SemanticExpr
    value: SemanticExpr | None = None


@dataclass(frozen=True)
class ScopedAggregateExpr:
    measure: str
    aggregation: str = ""
    temporal_role: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    where: list[dict[str, Any]] = field(default_factory=list)
    predicates: list[dict[str, Any]] = field(default_factory=list)
    null_behavior: str = ""
    # Per-row event-anchored window (round-three Phase 2). When set,
    # the lowering path materialises an anchor CTE and constrains the
    # measure source to events within the per-entity window.
    # ``anchor`` shape: {temporal_role: <role id>} — points at a
    # per-entity scalar temporal role (e.g.
    # ``temporal_role.jaffle_customer_first_order_at``).
    # ``window`` shape: {unit, value, direction?}; direction defaults
    # to ``"forward"``. Half-specified shapes are rejected at parse.
    anchor: dict[str, Any] = field(default_factory=dict)
    window: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RatioExpr:
    numerator: SemanticExpr
    denominator: SemanticExpr
    null_behavior: str = "null_if_zero"


@dataclass(frozen=True)
class EntityValueExpr:
    entity: str
    input: SemanticExpr
    where: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class DistributionExpr:
    function: str
    over: EntityValueExpr
    p: float | None = None
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConversionExpr:
    base: SemanticExpr
    converted: SemanticExpr
    entity: str = ""
    window_unit: str = ""
    window_value: int = 0
    matching_mode: str = ""
    constant_properties: list[str] = field(default_factory=list)
    dimension_bindings: dict[str, dict[str, str]] = field(default_factory=dict)


SemanticExpr = (
    MeasureRefExpr
    | AggregateExpr
    | MetricRecipeRefExpr
    | ColumnRefExpr
    | LiteralExpr
    | ArithmeticExpr
    | ComparisonExpr
    | BooleanExpr
    | CallExpr
    | DateAddExpr
    | InExpr
    | CaseExpr
    | CumulativeExpr
    | RollingExpr
    | PriorPeriodExpr
    | PeriodToDateExpr
    | OffsetWindowExpr
    | MetricPredicateExpr
    | ConditionalAggregateExpr
    | ScopedAggregateExpr
    | RatioExpr
    | EntityValueExpr
    | DistributionExpr
    | ConversionExpr
)


def expr_to_dict(expr: SemanticExpr) -> dict[str, Any]:
    if isinstance(expr, MeasureRefExpr):
        out: dict[str, Any] = {
            "kind": "measure",
            "measure": expr.measure,
            "aggregation": expr.aggregation,
            "temporal_role": expr.temporal_role,
        }
        if expr.parameters:
            out["parameters"] = dict(expr.parameters)
        return out
    if isinstance(expr, AggregateExpr):
        out = {
            "kind": "aggregate",
            "measure": expr.measure,
            "aggregation": expr.aggregation,
            "temporal_role": expr.temporal_role,
        }
        if expr.parameters:
            out["parameters"] = dict(expr.parameters)
        if expr.filter:
            out["filter"] = dict(expr.filter)
        if expr.window:
            out["window"] = dict(expr.window)
        return out
    if isinstance(expr, MetricRecipeRefExpr):
        return {"kind": "metric", "metric": expr.metric_recipe}
    if isinstance(expr, ColumnRefExpr):
        return {"kind": "column", "column": expr.column, "entity": expr.entity, "table": expr.table}
    if isinstance(expr, LiteralExpr):
        return {"kind": "literal", "value": expr.value}
    if isinstance(expr, ArithmeticExpr):
        out = {
            "kind": "arithmetic",
            "op": expr.op,
            "left": expr_to_dict(expr.left),
            "right": expr_to_dict(expr.right),
        }
        if expr.null_behavior:
            out["null_behavior"] = expr.null_behavior
        return out
    if isinstance(expr, ComparisonExpr):
        return {
            "kind": "comparison",
            "op": expr.op,
            "left": expr_to_dict(expr.left),
            "right": expr_to_dict(expr.right),
        }
    if isinstance(expr, BooleanExpr):
        return {"kind": "boolean", "op": expr.op, "args": [expr_to_dict(arg) for arg in expr.args]}
    if isinstance(expr, CallExpr):
        return {
            "kind": "call",
            "name": expr.name,
            "args": [expr_to_dict(arg) for arg in expr.args],
            "distinct": expr.distinct,
        }
    if isinstance(expr, DateAddExpr):
        return {
            "kind": "date_add",
            "unit": expr.unit,
            "value": expr_to_dict(expr.value),
            "date": expr_to_dict(expr.date),
        }
    if isinstance(expr, InExpr):
        out = {
            "kind": "in",
            "expr": expr_to_dict(expr.expr),
            "values": [expr_to_dict(value) for value in expr.values],
        }
        if expr.negated:
            out["negated"] = True
        return out
    if isinstance(expr, CaseExpr):
        return {
            "kind": "case",
            "whens": [
                {"when": expr_to_dict(item.when), "then": expr_to_dict(item.then)}
                for item in expr.whens
            ],
            "else": expr_to_dict(expr.else_expr) if expr.else_expr is not None else None,
        }
    if isinstance(expr, CumulativeExpr):
        return {
            "kind": "cumulative",
            "input": expr_to_dict(expr.input),
            "partition_by": list(expr.partition_by),
        }
    if isinstance(expr, RollingExpr):
        return {
            "kind": "rolling",
            "input": expr_to_dict(expr.input),
            "window": {"unit": expr.unit, "value": expr.value},
            "partition_by": list(expr.partition_by),
        }
    if isinstance(expr, PriorPeriodExpr):
        return {
            "kind": "prior_period",
            "input": expr_to_dict(expr.input),
            "offset": {"unit": expr.unit, "value": expr.value},
        }
    if isinstance(expr, PeriodToDateExpr):
        return {
            "kind": "period_to_date",
            "input": expr_to_dict(expr.input),
            "period": expr.period,
            "partition_by": list(expr.partition_by),
        }
    if isinstance(expr, OffsetWindowExpr):
        if expr.kind == "cumulative":
            out = {
                "kind": "cumulative",
                "input": expr_to_dict(expr.input),
                "partition_by": list(expr.partition_by),
            }
            if expr.window_scope:
                out["window_scope"] = expr.window_scope
            return out
        if expr.kind == "rolling":
            return {
                "kind": "rolling",
                "input": expr_to_dict(expr.input),
                "window": {"unit": expr.unit, "value": expr.value},
                "partition_by": list(expr.partition_by),
            }
        if expr.kind == "prior_period":
            return {
                "kind": "prior_period",
                "input": expr_to_dict(expr.input),
                "offset": {"unit": expr.unit, "value": expr.value},
            }
        if expr.kind == "period_to_date":
            return {
                "kind": "period_to_date",
                "input": expr_to_dict(expr.input),
                "period": expr.period,
                "partition_by": list(expr.partition_by),
            }
        return {
            "kind": expr.kind,
            "input": expr_to_dict(expr.input),
            "partition_by": list(expr.partition_by),
        }
    if isinstance(expr, MetricPredicateExpr):
        out = {
            "kind": "metric_predicate",
            "input": expr_to_dict(expr.input),
            "entity": expr.entity,
            "op": expr.op,
            "value": expr.value,
        }
        if expr.scope_mode:
            out["scope_mode"] = expr.scope_mode
        if expr.time_grain:
            out["time_grain"] = expr.time_grain
        if expr.time_alignment:
            out["time_alignment"] = expr.time_alignment
        if expr.window_unit and expr.window_value:
            out["window"] = {"unit": expr.window_unit, "value": expr.window_value}
        return out
    if isinstance(expr, ConditionalAggregateExpr):
        out = {
            "kind": "aggregate_if",
            "aggregation": expr.aggregation,
            "condition": expr_to_dict(expr.condition),
        }
        if expr.value is not None:
            out["value"] = expr_to_dict(expr.value)
        return out
    if isinstance(expr, ScopedAggregateExpr):
        out = {
            "kind": "scoped_aggregate",
            "measure": expr.measure,
            "aggregation": expr.aggregation,
            "temporal_role": expr.temporal_role,
        }
        if expr.parameters:
            out["parameters"] = dict(expr.parameters)
        if expr.where:
            out["where"] = [dict(item) for item in expr.where]
        if expr.predicates:
            out["predicates"] = [dict(item) for item in expr.predicates]
        if expr.null_behavior:
            out["null_behavior"] = expr.null_behavior
        return out
    if isinstance(expr, RatioExpr):
        return {
            "kind": "ratio",
            "numerator": expr_to_dict(expr.numerator),
            "denominator": expr_to_dict(expr.denominator),
            "null_behavior": expr.null_behavior,
        }
    if isinstance(expr, EntityValueExpr):
        return {
            "kind": "entity_value",
            "entity": expr.entity,
            "input": expr_to_dict(expr.input),
            "where": [dict(item) for item in expr.where],
        }
    if isinstance(expr, DistributionExpr):
        out = {
            "kind": "distribution",
            "function": expr.function,
            "over": expr_to_dict(expr.over),
            "parameters": dict(expr.parameters),
        }
        if expr.p is not None:
            out["p"] = expr.p
        return out
    if isinstance(expr, ConversionExpr):
        out = {
            "kind": "conversion",
            "base": expr_to_dict(expr.base),
            "converted": expr_to_dict(expr.converted),
            "entity": expr.entity,
            "window": {"unit": expr.window_unit, "value": expr.window_value},
            "matching_mode": expr.matching_mode,
            "constant_properties": list(expr.constant_properties),
        }
        if expr.dimension_bindings:
            out["dimension_bindings"] = {
                key: dict(value) for key, value in expr.dimension_bindings.items()
            }
        return out
    raise TypeError(f"Unsupported expression: {type(expr)!r}")


def expr_kind(expr: SemanticExpr) -> str:
    return expr_to_dict(expr)["kind"]


def _parse_python_expr(text: str) -> SemanticExpr:
    try:
        node = pyast.parse(text, mode="eval").body
    except SyntaxError as exc:
        raise SemanticLayerError(
            "INVALID_EXPRESSION_AST", f"Invalid expression syntax '{text}'"
        ) from exc

    def _convert(node: pyast.AST) -> SemanticExpr:
        if isinstance(node, pyast.Name):
            return ColumnRefExpr(column=node.id)
        if isinstance(node, pyast.Attribute) and isinstance(node.value, pyast.Name):
            return ColumnRefExpr(table=node.value.id, column=node.attr)
        if isinstance(node, pyast.Constant):
            return LiteralExpr(value=node.value)
        if isinstance(node, pyast.UnaryOp) and isinstance(node.op, pyast.USub):
            return ArithmeticExpr(op="subtract", left=LiteralExpr(0), right=_convert(node.operand))
        if isinstance(node, pyast.BinOp):
            op_map = {
                pyast.Add: "add",
                pyast.Sub: "subtract",
                pyast.Mult: "multiply",
                pyast.Div: "divide",
            }
            op = op_map.get(type(node.op))
            if not op:
                raise SemanticLayerError(
                    "INVALID_EXPRESSION_AST", f"Unsupported binary operation in '{text}'"
                )
            return ArithmeticExpr(op=op, left=_convert(node.left), right=_convert(node.right))
        if isinstance(node, pyast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
            cmp_op = node.ops[0]
            right = node.comparators[0]
            if isinstance(cmp_op, (pyast.In, pyast.NotIn)):
                if isinstance(right, (pyast.List, pyast.Tuple, pyast.Set)):
                    values = [_convert(item) for item in right.elts]
                else:
                    values = [_convert(right)]
                if not values:
                    raise SemanticLayerError(
                        "INVALID_EXPRESSION_AST",
                        f"IN expressions require at least one value in '{text}'",
                    )
                return InExpr(
                    expr=_convert(node.left), values=values, negated=isinstance(cmp_op, pyast.NotIn)
                )
            cmp_op_map: dict[type[pyast.cmpop], str] = {
                pyast.Eq: "=",
                pyast.NotEq: "<>",
                pyast.Lt: "<",
                pyast.LtE: "<=",
                pyast.Gt: ">",
                pyast.GtE: ">=",
            }
            mapped = cmp_op_map.get(type(cmp_op))
            if mapped:
                return ComparisonExpr(op=mapped, left=_convert(node.left), right=_convert(right))
        raise SemanticLayerError(
            "INVALID_EXPRESSION_AST", f"Unsupported expression syntax '{text}'"
        )

    return _convert(node)


def parse_config_expression(raw: Any) -> SemanticExpr:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise SemanticLayerError("INVALID_CONFIG", "Measure expression cannot be empty")
        return _parse_python_expr(text)
    return parse_semantic_expression(raw, context="config")


def _normalize_arithmetic_op(op: Any) -> str:
    mapping = {
        "+": "add",
        "plus": "add",
        "add": "add",
        "-": "subtract",
        "minus": "subtract",
        "subtract": "subtract",
        "*": "multiply",
        "times": "multiply",
        "multiply": "multiply",
        "/": "divide",
        "divide": "divide",
    }
    text = str(op or "").strip().lower()
    return mapping.get(text, text)


# Valid top-level keys per expression `kind`. Used by
# :func:`_reject_unknown_expression_keys` to surface ``INVALID_EXPRESSION_KEY``
# with a ``closest_matches`` block when the caller mistypes a field
# name (e.g. ``meaure`` instead of ``measure``). The keys are derived
# from the dispatch arms in :func:`parse_semantic_expression` below
# — keep these tables in sync when adding a new expression shape.
_COMMON_KEYS = {"kind"}
_VALID_KEYS_BY_KIND: dict[str, set] = {
    "aggregate": _COMMON_KEYS
    | {"measure", "aggregation", "temporal_role", "parameters", "filter", "window"},
    "semi_additive": _COMMON_KEYS
    | {"measure", "aggregation", "temporal_role", "parameters", "filter", "window"},
    "measure": _COMMON_KEYS | {"measure", "aggregation", "temporal_role", "parameters"},
    "measure_ref": _COMMON_KEYS | {"measure", "aggregation", "temporal_role", "parameters"},
    "metric": _COMMON_KEYS | {"metric"},
    "column": _COMMON_KEYS | {"column", "entity", "table"},
    "literal": _COMMON_KEYS | {"value"},
    "arithmetic": _COMMON_KEYS | {"op", "left", "right", "null_behavior"},
    "binary": _COMMON_KEYS | {"op", "left", "right", "null_behavior"},
    "comparison": _COMMON_KEYS | {"op", "left", "right"},
    "boolean": _COMMON_KEYS | {"op", "args"},
    "call": _COMMON_KEYS | {"name", "args", "distinct"},
    "date_add": _COMMON_KEYS | {"unit", "value", "date"},
    "in": _COMMON_KEYS | {"expr", "left", "values", "negated"},
    "not_in": _COMMON_KEYS | {"expr", "left", "values", "negated"},
    # ``between`` / ``not_between`` are parse-time sugar that desugar to
    # ``BooleanExpr`` folds of two ``ComparisonExpr`` nodes. They appear
    # in :func:`parse_semantic_expression` and never reach the AST/IR.
    "between": _COMMON_KEYS | {"expr", "low", "high", "negated"},
    "not_between": _COMMON_KEYS | {"expr", "low", "high", "negated"},
    "nullif": _COMMON_KEYS | {"value", "null_value"},
    "case": _COMMON_KEYS | {"whens", "else"},
    "cumulative": _COMMON_KEYS | {"input", "partition_by", "window_scope"},
    "rolling": _COMMON_KEYS | {"input", "window", "partition_by"},
    # ``prior_period`` accepts two shapes:
    #   1. IR shape (canonical): ``{kind, input, offset: {unit, value}}``
    #   2. User-facing shorthand: ``{kind, measure, offset: <int>, grain, aggregation?}``
    # The shorthand normalises to the IR shape inside ``parse_semantic_expression``.
    # ``aggregation`` is optional and defaults to ``sum``.
    "prior_period": _COMMON_KEYS | {"input", "offset", "measure", "grain", "aggregation"},
    "period_to_date": _COMMON_KEYS | {"input", "period", "partition_by"},
    "metric_predicate": _COMMON_KEYS
    | {"input", "entity", "op", "value", "scope_mode", "time_grain", "time_alignment", "window"},
    "aggregate_if": _COMMON_KEYS | {"aggregation", "condition", "value"},
    "scoped_aggregate": _COMMON_KEYS
    | {
        "measure",
        "aggregation",
        "temporal_role",
        "parameters",
        "where",
        "predicates",
        "null_behavior",
        # Round 3: per-row event-anchored window keys. SQL lowering
        # is staged; parser + validator land here.
        "anchor",
        "window",
    },
    "ratio": _COMMON_KEYS | {"numerator", "denominator", "null_behavior"},
    "entity_value": _COMMON_KEYS | {"entity", "input", "where"},
    "distribution": _COMMON_KEYS | {"function", "over", "p", "parameters"},
    "conversion": _COMMON_KEYS
    | {
        "base",
        "converted",
        "entity",
        "window",
        "matching_mode",
        "matching",
        "constant_properties",
        "dimension_bindings",
    },
}


# Schema for the inner ``parameters`` dict, keyed by aggregation name.
# Mirrors ``_VALID_KEYS_BY_KIND`` in shape — the recovery hint surfaces
# the matching entry when the agent gets the outer ``parameters`` key
# right but the inner shape wrong. Currently only ``percentile`` reads
# parameters (see ``compiler_parts/bind.py:_aggregation_expr``); add
# new aggregations here as they grow parameter requirements.
_PARAMETER_SCHEMAS_BY_AGGREGATION: dict[str, dict[str, str]] = {
    "percentile": {"p": "float in [0, 1]"},
}

# Units the conversion matching window can lower into the dialects'
# DATE_DIFF. Matches the units advertised in the window-shape recovery
# hint below; anything else only fails at warehouse execution time.
_CONVERSION_WINDOW_UNITS = {"minute", "hour", "day", "week", "month", "quarter", "year"}


def parameter_schema_for_aggregation(aggregation: str) -> dict[str, str]:
    """Public accessor for ``_PARAMETER_SCHEMAS_BY_AGGREGATION``.

    Returns ``{}`` for aggregations with no parameter requirements
    (sum, count, avg, min, max, median, ...). Used by the diagnostics
    recovery-hint layer to disclose nested shapes.
    """
    return dict(_PARAMETER_SCHEMAS_BY_AGGREGATION.get(str(aggregation or "").lower(), {}))


def _closest_key_matches(name: str, candidates: Iterable[str], *, limit: int = 3) -> list[str]:
    from difflib import get_close_matches

    return get_close_matches(str(name), [str(c) for c in candidates], n=limit, cutoff=0.5)


def _reject_unknown_expression_keys(expr: dict[str, Any], *, kind: str, context: str) -> None:
    """Raise ``INVALID_EXPRESSION_KEY`` when an expression dict carries a
    top-level key that the kind's dispatch arm does not recognise. This
    surfaces typos (e.g. ``meaure`` for ``measure``) with a structured
    ``closest_matches`` block instead of letting the value get silently
    dropped during normalization.
    """
    valid = _VALID_KEYS_BY_KIND.get(kind)
    if not valid:
        return
    unknown = sorted(set(expr.keys()) - valid)
    if not unknown:
        return
    # Pick the first unknown key for closest_matches — agents typically
    # mistype one field per retry, and listing every match for every
    # unknown key bloats the envelope.
    first = unknown[0]
    # Pass the aggregation through details so the diagnostics layer can
    # disclose the matching parameter schema without re-parsing the
    # expression. ``aggregation`` is the only field readers need to
    # decide whether to attach a parameters_schema hint.
    aggregation = str(expr.get("aggregation", "") or "").strip()
    raise SemanticLayerError(
        "INVALID_EXPRESSION_KEY",
        f"{context} expression with kind={kind!r} has unsupported key(s): {unknown}",
        details={
            "expression_kind": kind,
            "expression_position": context,
            "unknown_keys": unknown,
            "valid_keys": sorted(valid),
            "closest_matches": _closest_key_matches(first, valid),
            "aggregation": aggregation,
        },
    )


def expression_field(
    expr: Any,
    key: str,
    *,
    expression_position: str,
) -> Any:
    """Return ``expr[key]`` or raise a structured ``INVALID_EXPRESSION``
    error. Replaces bare ``expr['field']`` lookups that previously
    leaked as ``MCP error -32603: 'field'`` (a Python ``KeyError``)
    through the JSON-RPC layer with no recovery hint.

    ``expression_position`` names the IR position the caller is parsing
    (``"order_by"``, ``"metric_filters"``, etc.) so the recovery hint can
    point at the right shape.
    """
    if not isinstance(expr, dict):
        raise SemanticLayerError(
            "INVALID_EXPRESSION",
            f"{expression_position} entry must be an object with a {key!r} key",
            details={
                "expression_position": expression_position,
                "missing_key": key,
                "received_type": type(expr).__name__,
            },
        )
    if key not in expr:
        raise SemanticLayerError(
            "INVALID_EXPRESSION",
            f"{expression_position} entry is missing the required {key!r} key",
            details={
                "expression_position": expression_position,
                "missing_key": key,
                "received_keys": sorted(str(k) for k in expr),
            },
        )
    return expr[key]


def parse_semantic_expression(raw: Any, *, context: str) -> SemanticExpr:
    if not isinstance(raw, dict):
        raise SemanticLayerError(
            "INVALID_EXPRESSION_AST", f"{context} expression must be an object"
        )
    expr = dict(raw)
    kind = str(expr.get("kind", "")).strip()
    # Infer the "effective kind" for unknown-key checking when the
    # caller used a kindless shorthand (``{"measure": ...}`` or
    # ``{"metric": ...}``). The actual dispatch below still owns
    # the parsing; this only shapes the unknown-key error.
    effective_kind = kind
    if not effective_kind:
        if "measure" in expr:
            effective_kind = "measure"
        elif "metric" in expr:
            effective_kind = "metric"
    if effective_kind:
        _reject_unknown_expression_keys(expr, kind=effective_kind, context=context)
    if kind in {"aggregate", "semi_additive"}:
        measure_id = str(expr.get("measure", "")).strip()
        if not measure_id:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST", f"{kind} expressions require 'measure'"
            )
        return AggregateExpr(
            measure=measure_id,
            aggregation=str(expr.get("aggregation", "")).strip(),
            temporal_role=str(expr.get("temporal_role", "")).strip(),
            parameters=dict(expr.get("parameters", {}) or {}),
            filter=dict(expr.get("filter", {}) or {}),
            window=dict(expr.get("window", {}) or {}),
        )
    if kind == "aggregate_if":
        aggregation = str(expr.get("aggregation", "")).strip()
        if not aggregation:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST",
                "aggregate_if expressions require 'aggregation' (count, sum, avg, min, max, …)",
            )
        if "condition" not in expr:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST", "aggregate_if expressions require 'condition'"
            )
        condition = parse_semantic_expression(expr.get("condition"), context=context)
        raw_value = expr.get("value")
        if raw_value is None:
            if aggregation.lower() != "count":
                raise SemanticLayerError(
                    "INVALID_EXPRESSION_AST",
                    (
                        f"aggregate_if({aggregation}, …) requires a 'value' expression — "
                        "only 'count' may omit value"
                    ),
                    details={"aggregation": aggregation},
                )
            value = None
        else:
            value = parse_semantic_expression(raw_value, context=context)
        return ConditionalAggregateExpr(aggregation=aggregation, condition=condition, value=value)
    # ``prior_period`` (and other window kinds) accept ``measure`` as part
    # of a shorthand shape — falling into the MeasureRefExpr arm here
    # would silently drop the ``kind``/``offset`` fields. Exclude window
    # kinds so the dispatch reaches their own parsing arm below.
    if (
        "measure" in expr
        and kind
        not in {"scoped_aggregate", "prior_period", "rolling", "cumulative", "period_to_date"}
    ) or kind in {"measure", "measure_ref"}:
        measure_id = str(expr.get("measure", "")).strip()
        if not measure_id:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST", "Measure expressions require 'measure'"
            )
        return MeasureRefExpr(
            measure=measure_id,
            aggregation=str(expr.get("aggregation", "")).strip(),
            temporal_role=str(expr.get("temporal_role", "")).strip(),
            parameters=dict(expr.get("parameters", {}) or {}),
        )
    if kind == "metric" or ("metric" in expr and not kind and "measure" not in expr):
        metric_id = str(expr.get("metric", "")).strip()
        if not metric_id:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST", "Metric expressions require a 'metric' field"
            )
        return MetricRecipeRefExpr(metric_recipe=metric_id)
    if not kind:
        # Most common mistake at this point: agent put `{dimension: "..."}`
        # in select[]. Dimensions belong in `group_by[]`, not select.
        # Surface a targeted hint instead of the generic shape error.
        if "dimension" in expr:
            dim_id = str(expr.get("dimension", "") or "")
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST",
                (
                    "Expression has 'dimension' but no 'kind'. Dimensions "
                    "are not select expressions — they belong on "
                    "`group_by[]` as bare string ids."
                ),
                details={
                    "expression_position": context,
                    "path": "expression",
                    "received_keys": sorted(expr.keys()),
                    "recovery_hints": [
                        {
                            "code": "MOVE_DIMENSION_TO_GROUP_BY",
                            "message": (
                                f"Move '{dim_id}' from select[].expression to "
                                "the top-level `group_by[]` array as a bare "
                                "string. Measures/metrics go in select[]; "
                                "dimensions group those aggregates."
                            ),
                            "suggested_query_ir_change": {
                                "remove_select_item": {"dimension": dim_id},
                                "add_to_group_by": dim_id,
                            },
                        }
                    ],
                },
            )
        raise SemanticLayerError("INVALID_EXPRESSION_AST", "Expression requires a 'kind'")

    if kind == "column":
        column = str(expr.get("column", "")).strip()
        if not column:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST", "Column expressions require 'column'"
            )
        return ColumnRefExpr(
            column=column,
            entity=str(expr.get("entity", "")).strip(),
            table=str(expr.get("table", "")).strip(),
        )
    if kind == "literal":
        return LiteralExpr(value=expr.get("value"))
    if kind in {"arithmetic", "binary"}:
        return ArithmeticExpr(
            op=_normalize_arithmetic_op(expr.get("op", "")),
            left=parse_semantic_expression(expr.get("left"), context=context),
            right=parse_semantic_expression(expr.get("right"), context=context),
            null_behavior=str(expr.get("null_behavior", "")).strip(),
        )
    if kind == "comparison":
        return ComparisonExpr(
            op=str(expr.get("op", "")).strip(),
            left=parse_semantic_expression(expr.get("left"), context=context),
            right=parse_semantic_expression(expr.get("right"), context=context),
        )
    if kind == "boolean":
        raw_op = str(expr.get("op", "")).strip()
        op = raw_op.lower()
        if op not in {"and", "or", "not"}:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST",
                f"Unsupported boolean op '{raw_op}'",
                details={
                    "expression_kind": "boolean",
                    "expression_position": context,
                    "path": "expression.op",
                    "received_value": raw_op,
                    "allowed": ["and", "or", "not"],
                    "closest_matches": _closest_key_matches(op, ["and", "or", "not"]),
                    "recovery_hints": [
                        {
                            "code": "USE_SUPPORTED_BOOLEAN_OP",
                            "message": (
                                "Boolean expressions support op 'and', 'or', or 'not' "
                                "(case-insensitive). For negated membership use kind "
                                "'not_in'; for negated ranges use kind 'not_between'."
                            ),
                        }
                    ],
                },
            )
        args = [
            parse_semantic_expression(arg, context=context)
            for arg in list(expr.get("args", []) or [])
        ]
        if not args:
            raise SemanticLayerError("INVALID_EXPRESSION_AST", "Boolean expressions require args")
        if op == "not" and len(args) != 1:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST",
                f"Boolean 'not' expressions require exactly one arg, got {len(args)}",
                details={
                    "expression_kind": "boolean",
                    "expression_position": context,
                    "path": "expression.args",
                    "received_arg_count": len(args),
                    "recovery_hints": [
                        {
                            "code": "WRAP_NOT_ARGS",
                            "message": (
                                "'not' is unary. To negate several conditions at once, "
                                "wrap them in a single {kind: 'boolean', op: 'and', "
                                "args: [...]} (or 'or') and pass that as the only arg."
                            ),
                        }
                    ],
                },
            )
        return BooleanExpr(op=op, args=args)
    if kind == "call":
        name = str(expr.get("name", "")).strip()
        if not name:
            raise SemanticLayerError("INVALID_EXPRESSION_AST", "Call expressions require 'name'")
        return CallExpr(
            name=name,
            args=[
                parse_semantic_expression(arg, context=context)
                for arg in list(expr.get("args", []) or [])
            ],
            distinct=bool(expr.get("distinct", False)),
        )
    if kind == "date_add":
        unit = str(expr.get("unit", "")).strip()
        if not unit:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST", "date_add expressions require 'unit'"
            )
        if "value" not in expr or "date" not in expr:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST", "date_add expressions require 'value' and 'date'"
            )
        return DateAddExpr(
            unit=unit,
            value=parse_semantic_expression(expr.get("value"), context=context),
            date=parse_semantic_expression(expr.get("date"), context=context),
        )
    if kind in {"in", "not_in"}:
        raw_values = list(expr.get("values", []) or [])
        if not raw_values:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST", "in expressions require non-empty 'values'"
            )
        values = [
            parse_semantic_expression(value, context=context)
            if isinstance(value, dict)
            else LiteralExpr(value)
            for value in raw_values
        ]
        return InExpr(
            expr=parse_semantic_expression(expr.get("expr", expr.get("left")), context=context),
            values=values,
            negated=bool(expr.get("negated", False)) or kind == "not_in",
        )
    if kind in {"between", "not_between"}:
        # Parse-time sugar. ``between`` lowers to AND of two comparisons;
        # ``not_between`` lowers to OR of the inverted comparisons (rather
        # than wrapping AND in a NOT, since ``BooleanExpr`` is folded as
        # a binary AND/OR — see ``_config_expr_to_sql`` in bind.py — and
        # has no unary-NOT support).
        if "expr" not in expr:
            raise SemanticLayerError("INVALID_EXPRESSION_AST", f"{kind} expressions require 'expr'")
        if "low" not in expr or "high" not in expr:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST", f"{kind} expressions require 'low' and 'high'"
            )
        target = parse_semantic_expression(expr.get("expr"), context=context)
        low = parse_semantic_expression(expr.get("low"), context=context)
        high = parse_semantic_expression(expr.get("high"), context=context)
        negated = bool(expr.get("negated", False)) or kind == "not_between"
        if negated:
            return BooleanExpr(
                op="or",
                args=[
                    ComparisonExpr(op="<", left=target, right=low),
                    ComparisonExpr(op=">", left=target, right=high),
                ],
            )
        return BooleanExpr(
            op="and",
            args=[
                ComparisonExpr(op=">=", left=target, right=low),
                ComparisonExpr(op="<=", left=target, right=high),
            ],
        )
    if kind == "nullif":
        return CallExpr(
            name="NULLIF",
            args=[
                parse_semantic_expression(expr.get("value"), context=context),
                parse_semantic_expression(expr.get("null_value"), context=context),
            ],
        )
    if kind == "case":
        whens = []
        for row in list(expr.get("whens", []) or []):
            whens.append(
                CaseWhenExpr(
                    when=parse_semantic_expression(row.get("when"), context=context),
                    then=parse_semantic_expression(row.get("then"), context=context),
                )
            )
        if not whens:
            raise SemanticLayerError("INVALID_EXPRESSION_AST", "Case expressions require whens")
        else_expr = expr.get("else")
        return CaseExpr(
            whens=whens,
            else_expr=parse_semantic_expression(else_expr, context=context)
            if else_expr is not None
            else None,
        )
    if kind == "cumulative":
        return OffsetWindowExpr(
            input=parse_semantic_expression(expr.get("input"), context=context),
            kind="cumulative",
            aggregate="sum",
            frame="ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
            partition_by=[str(item) for item in list(expr.get("partition_by", []) or [])],
            window_scope=str(expr.get("window_scope", "")).strip(),
        )
    if kind == "rolling":
        raw_window = expr.get("window", {}) or {}
        if not isinstance(raw_window, dict):
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST",
                "Rolling expressions require window as an object {unit, value}",
                details={
                    "expression_kind": "rolling",
                    "expression_position": context,
                    "path": "expression.window",
                    "received_type": type(raw_window).__name__,
                    "received_value": raw_window,
                    "recovery_hints": [
                        {
                            "code": "USE_OBJECT_SHAPE",
                            "message": (
                                "Wrap window as {unit, value}. E.g. window=7, grain='day' "
                                "→ window={'unit': 'day', 'value': 7}. The 'grain' key is "
                                "not accepted on rolling — only unit + value."
                            ),
                            "suggested_shape": {
                                "unit": "<minute|hour|day|week|month|quarter|year>",
                                "value": "<positive integer>",
                            },
                        }
                    ],
                },
            )
        window = dict(raw_window)
        unit = str(window.get("unit", "")).strip()
        window_value = int(window.get("value", 0) or 0)
        if not unit or window_value <= 0:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST",
                "Rolling expressions require a positive window.unit and window.value",
            )
        return OffsetWindowExpr(
            input=parse_semantic_expression(expr.get("input"), context=context),
            kind="rolling",
            aggregate="sum",
            frame="rows",
            unit=unit,
            value=window_value,
            partition_by=[str(item) for item in list(expr.get("partition_by", []) or [])],
        )
    if kind == "prior_period":
        # Two accepted shapes (see ``_VALID_KEYS_BY_KIND`` above):
        #   1. IR shape: ``{kind, input: <expr>, offset: {unit, value}}``
        #   2. User-facing shorthand: ``{kind, measure: <id>, offset: <int>, grain: <unit>, aggregation?}``
        # The shorthand is what the reviewer-facing pitch promises ("inline
        # period shifts work") and what the curated metric YAML uses; the
        # IR shape is what config-level recipe expansion emits. Detect the
        # shorthand by the presence of ``measure`` (a string id) and a
        # scalar (int) ``offset``; otherwise fall through to the IR shape.
        is_shorthand = (
            "measure" in expr
            and isinstance(expr.get("measure"), str)
            and not isinstance(expr.get("offset"), dict)
        )
        if is_shorthand:
            measure_id = str(expr.get("measure", "")).strip()
            if not measure_id:
                raise SemanticLayerError(
                    "INVALID_EXPRESSION_AST",
                    "Prior-period (shorthand) expressions require a non-empty 'measure'",
                )
            raw_offset = expr.get("offset", -1)
            try:
                offset_int = int(raw_offset)
            except (TypeError, ValueError) as exc:
                raise SemanticLayerError(
                    "INVALID_EXPRESSION_AST",
                    "Prior-period 'offset' (shorthand) must be an integer",
                    details={"received": raw_offset},
                ) from exc
            if offset_int == 0:
                raise SemanticLayerError(
                    "INVALID_EXPRESSION_AST",
                    "Prior-period 'offset' (shorthand) must be a non-zero integer",
                    details={"received": offset_int},
                )
            unit = str(expr.get("grain", "")).strip()
            if not unit:
                raise SemanticLayerError(
                    "INVALID_EXPRESSION_AST",
                    "Prior-period (shorthand) expressions require a 'grain' (e.g. year, quarter, month, week, day)",
                )
            aggregation = str(expr.get("aggregation", "sum")).strip() or "sum"
            inner = AggregateExpr(
                measure=measure_id,
                aggregation=aggregation,
                temporal_role="",
                parameters={},
                filter={},
                window={},
            )
            return OffsetWindowExpr(
                input=inner,
                kind="prior_period",
                aggregate="lag",
                unit=unit,
                value=abs(offset_int),
            )
        raw_offset = expr.get("offset", {}) or {}
        if not isinstance(raw_offset, dict):
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST",
                "Prior-period (IR shape) expressions require offset as an object {unit, value}",
                details={
                    "expression_kind": "prior_period",
                    "expression_position": context,
                    "path": "expression.offset",
                    "received_type": type(raw_offset).__name__,
                    "received_value": raw_offset,
                    "recovery_hints": [
                        {
                            "code": "USE_OBJECT_SHAPE",
                            "message": (
                                "IR-shape prior_period takes offset as an object. For an "
                                "inline shift use the shorthand: {kind: 'prior_period', "
                                "measure: '<id>', offset: <signed int>, grain: '<unit>'}. "
                                "For the IR shape use offset: {unit, value}."
                            ),
                            "suggested_shape": {
                                "unit": "<day|week|month|quarter|year>",
                                "value": "<positive integer>",
                            },
                        }
                    ],
                },
            )
        offset = dict(raw_offset)
        # Reject offset dicts that carry unknown keys (``n``, ``count``,
        # ``period``, ...). The canonical offset shape is exactly
        # ``{unit, value}``. Without this gate, a typo like
        # ``offset: {unit: "month", n: 1}`` silently defaulted ``value=1``
        # and then crashed downstream with "expression must be an object"
        # because ``input`` was missing — the reviewer's exact footgun.
        _VALID_OFFSET_KEYS = {"unit", "value"}
        unknown_offset_keys = sorted(set(offset.keys()) - _VALID_OFFSET_KEYS)
        if unknown_offset_keys:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST",
                (
                    "Prior-period 'offset' has unsupported key(s) "
                    f"{unknown_offset_keys}. The canonical shape is "
                    "{unit: <day|week|month|quarter|year>, value: <positive integer>}."
                ),
                details={
                    "expression_kind": "prior_period",
                    "expression_position": context,
                    "unknown_offset_keys": unknown_offset_keys,
                    "valid_offset_keys": sorted(_VALID_OFFSET_KEYS),
                    "received": dict(expr),
                },
            )
        unit = str(offset.get("unit", "")).strip()
        try:
            offset_value = int(offset.get("value", 0) or 0)
        except (TypeError, ValueError):
            offset_value = 0
        if not unit or offset_value <= 0:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST",
                "Prior-period expressions require a positive offset.unit and offset.value",
                details={
                    "expression_kind": "prior_period",
                    "expression_position": context,
                    "received": dict(expr),
                },
            )
        if "input" not in expr or not isinstance(expr.get("input"), dict):
            # Without ``input``, the IR-shape path used to recurse on
            # ``None`` and crash with the generic "expression must be an
            # object". Surface a targeted hint instead — including the
            # shorthand the agent probably meant.
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST",
                (
                    "Prior-period expressions in IR shape require an "
                    "'input' expression (e.g. {kind: 'measure', "
                    "measure: '<measure_id>'}). For an inline shift use the "
                    "shorthand: {kind: 'prior_period', measure: '<id>', "
                    "offset: <signed int>, grain: '<unit>'}."
                ),
                details={
                    "expression_kind": "prior_period",
                    "expression_position": context,
                    "missing_key": "input",
                    "received": dict(expr),
                },
            )
        return OffsetWindowExpr(
            input=parse_semantic_expression(expr.get("input"), context=context),
            kind="prior_period",
            aggregate="lag",
            unit=unit,
            value=offset_value,
        )
    if kind == "period_to_date":
        period = str(expr.get("period", "")).strip()
        if not period:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST", "Period-to-date expressions require 'period'"
            )
        return OffsetWindowExpr(
            input=parse_semantic_expression(expr.get("input"), context=context),
            kind="period_to_date",
            aggregate="sum",
            frame="ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
            extra_partition="period",
            period=period,
            partition_by=[str(item) for item in list(expr.get("partition_by", []) or [])],
        )
    if kind == "metric_predicate":
        entity = str(expr.get("entity", "")).strip()
        op = str(expr.get("op", "")).strip()
        scope_mode = str(expr.get("scope_mode", "")).strip()
        time_grain = str(expr.get("time_grain", "")).strip()
        time_alignment = str(expr.get("time_alignment", "")).strip()
        window = dict(expr.get("window", {}) or {})
        window_unit = str(window.get("unit", "")).strip()
        window_value = int(window.get("value", 0) or 0)
        if not entity:
            raise SemanticLayerError(
                "PREDICATE_ENTITY_REQUIRED", "metric_predicate expressions require 'entity'"
            )
        if "input" not in expr:
            raise SemanticLayerError(
                "PREDICATE_INPUT_REQUIRED", "metric_predicate expressions require 'input'"
            )
        if not op:
            raise SemanticLayerError(
                "INVALID_METRIC_PREDICATE", "metric_predicate expressions require 'op'"
            )
        if context == "config" and not scope_mode:
            raise SemanticLayerError(
                "INVALID_METRIC_PREDICATE",
                "package-authored metric_predicate expressions require 'scope_mode'",
            )
        if context == "query" and not scope_mode:
            scope_mode = "contextual"
        if scope_mode not in {"contextual", "entity_only"}:
            raise SemanticLayerError(
                "INVALID_METRIC_PREDICATE",
                "metric_predicate scope_mode must be 'contextual' or 'entity_only'",
            )
        if scope_mode == "entity_only" and time_grain:
            raise SemanticLayerError(
                "INVALID_METRIC_PREDICATE",
                "entity_only metric_predicate expressions cannot declare time_grain",
            )
        if time_alignment and time_alignment not in {
            "same_query_period",
            "query_window",
            "rolling_window_in_period",
        }:
            raise SemanticLayerError(
                "INVALID_METRIC_PREDICATE",
                "metric_predicate time_alignment must be 'same_query_period', 'query_window', or 'rolling_window_in_period'",
            )
        if time_alignment == "same_query_period" and scope_mode != "contextual":
            raise SemanticLayerError(
                "INVALID_METRIC_PREDICATE",
                "metric_predicate time_alignment 'same_query_period' requires contextual scope",
            )
        if (
            time_alignment in {"query_window", "rolling_window_in_period"}
            and scope_mode != "entity_only"
        ):
            raise SemanticLayerError(
                "INVALID_METRIC_PREDICATE",
                "metric_predicate time_alignment is only supported for entity_only bounded-window predicates",
            )
        # ``value`` accepts either a literal or an expression-shaped
        # dict for inline thresholds. The only expression kind supported
        # today is ``percentile`` — wider support waits until the
        # use-cases prove out. The lowering path inspects this shape at
        # SQL-emission time.
        raw_value = expr.get("value")
        validated_value = raw_value
        if isinstance(raw_value, dict):
            value_kind = str(raw_value.get("kind", "") or "").strip()
            if value_kind == "percentile":
                # Validate p ∈ [0, 1] at parse time so the error fires
                # at validate, not at compile/execute.
                if "p" not in raw_value:
                    raise SemanticLayerError(
                        "INVALID_EXPRESSION_AST",
                        "metric_predicate inline percentile threshold requires 'p'",
                        details={
                            "expression_kind": "metric_predicate",
                            "expression_position": context,
                            "received_value": dict(raw_value),
                            "missing_key": "p",
                        },
                    )
                try:
                    p = float(raw_value["p"])
                except (TypeError, ValueError) as exc:
                    raise SemanticLayerError(
                        "INVALID_EXPRESSION_AST",
                        "metric_predicate inline percentile threshold 'p' must be numeric",
                        details={"received": raw_value.get("p")},
                    ) from exc
                if not 0.0 <= p <= 1.0:
                    raise SemanticLayerError(
                        "INVALID_EXPRESSION_AST",
                        "metric_predicate inline percentile threshold 'p' must be in [0, 1]",
                        details={"received": p},
                    )
                validated_value = {"kind": "percentile", "p": p}
                if "measure" in raw_value:
                    validated_value["measure"] = str(raw_value["measure"])
            elif value_kind:
                # Other expression kinds are not yet supported as inline
                # thresholds. Surface a clear envelope rather than letting
                # the dict pass through to a SqlLiteral wrap (which would
                # produce a broken SQL string).
                raise SemanticLayerError(
                    "INVALID_EXPRESSION_AST",
                    (
                        f"metric_predicate inline threshold of kind={value_kind!r} "
                        "is not supported yet. Supported inline shape: "
                        "{kind: 'percentile', p: <float in [0,1]>, measure?: '<id>'}. "
                        "For other thresholds, compute the value in a "
                        "separate query and pass it as a literal."
                    ),
                    details={
                        "expression_kind": "metric_predicate",
                        "expression_position": context,
                        "received_value_kind": value_kind,
                        "supported_inline_kinds": ["percentile"],
                    },
                )
        return MetricPredicateExpr(
            input=parse_semantic_expression(expr.get("input"), context=context),
            entity=entity,
            op=op,
            value=validated_value,
            scope_mode=scope_mode,
            time_grain=time_grain,
            time_alignment=time_alignment,
            window_unit=window_unit,
            window_value=window_value,
        )
    if kind == "scoped_aggregate":
        measure_id = str(expr.get("measure", "")).strip()
        if not measure_id:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST", "scoped_aggregate expressions require 'measure'"
            )
        # Per-row event-anchored window: anchor + window must be
        # specified together. Half-specified shapes are a common typo;
        # rejecting at parse-time avoids a confusing downstream error.
        raw_anchor = expr.get("anchor")
        raw_window = expr.get("window")
        has_anchor = isinstance(raw_anchor, dict) and bool(raw_anchor)
        has_window = isinstance(raw_window, dict) and bool(raw_window)
        if has_anchor != has_window:
            missing = "window" if has_anchor else "anchor"
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST",
                (
                    f"scoped_aggregate with {'anchor' if has_anchor else 'window'} "
                    f"requires the matching {missing!r} key. Per-row event-"
                    "anchored windows need both: anchor names the per-entity "
                    "scalar (e.g. customer.first_order_at), window names the "
                    "bound (e.g. {unit: day, value: 90, direction: forward})."
                ),
                details={
                    "expression_kind": "scoped_aggregate",
                    "expression_position": context,
                    "missing_key": missing,
                    "received_keys": sorted(expr.keys()),
                },
            )
        anchor_payload: dict[str, Any] = {}
        window_payload: dict[str, Any] = {}
        if has_anchor and has_window:
            assert isinstance(raw_anchor, dict)  # narrowed by has_anchor check
            assert isinstance(raw_window, dict)  # narrowed by has_window check
            anchor_role = str(raw_anchor.get("temporal_role", "") or "").strip()
            if not anchor_role:
                raise SemanticLayerError(
                    "INVALID_EXPRESSION_AST",
                    "scoped_aggregate.anchor requires 'temporal_role'",
                    details={
                        "expression_kind": "scoped_aggregate",
                        "expression_position": context,
                        "missing_key": "anchor.temporal_role",
                    },
                )
            window_unit = str(raw_window.get("unit", "")).strip().lower()
            if window_unit not in {"day", "week", "month", "quarter", "year"}:
                raise SemanticLayerError(
                    "INVALID_EXPRESSION_AST",
                    "scoped_aggregate.window.unit must be one of day/week/month/quarter/year",
                    details={
                        "expression_kind": "scoped_aggregate",
                        "expression_position": context,
                        "received_unit": str(raw_window.get("unit", "")),
                    },
                )
            try:
                window_value = int(raw_window.get("value", 0) or 0)
            except (TypeError, ValueError):
                window_value = 0
            if window_value <= 0:
                raise SemanticLayerError(
                    "INVALID_EXPRESSION_AST",
                    "scoped_aggregate.window.value must be a positive integer",
                    details={
                        "expression_kind": "scoped_aggregate",
                        "expression_position": context,
                        "received_value": raw_window.get("value"),
                    },
                )
            window_direction = (
                str(raw_window.get("direction", "forward")).strip().lower() or "forward"
            )
            if window_direction not in {"forward", "backward", "around"}:
                raise SemanticLayerError(
                    "INVALID_EXPRESSION_AST",
                    "scoped_aggregate.window.direction must be one of forward/backward/around",
                    details={
                        "expression_kind": "scoped_aggregate",
                        "expression_position": context,
                        "received_direction": str(raw_window.get("direction", "")),
                    },
                )
            anchor_payload = {"temporal_role": anchor_role}
            window_payload = {
                "unit": window_unit,
                "value": window_value,
                "direction": window_direction,
            }
        return ScopedAggregateExpr(
            measure=measure_id,
            aggregation=str(expr.get("aggregation", "")).strip(),
            temporal_role=str(expr.get("temporal_role", "")).strip(),
            parameters=dict(expr.get("parameters", {}) or {}),
            where=[dict(item) for item in list(expr.get("where", []) or [])],
            predicates=[dict(item) for item in list(expr.get("predicates", []) or [])],
            null_behavior=str(expr.get("null_behavior", "")).strip(),
            anchor=anchor_payload,
            window=window_payload,
        )
    if kind == "ratio":
        if "numerator" not in expr or "denominator" not in expr:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST", "ratio expressions require numerator and denominator"
            )
        return RatioExpr(
            numerator=parse_semantic_expression(expr.get("numerator"), context=context),
            denominator=parse_semantic_expression(expr.get("denominator"), context=context),
            null_behavior=str(expr.get("null_behavior", "null_if_zero") or "null_if_zero"),
        )
    if kind == "entity_value":
        entity = str(expr.get("entity", "")).strip()
        if not entity:
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST", "entity_value expressions require 'entity'"
            )
        return EntityValueExpr(
            entity=entity,
            input=parse_semantic_expression(expr.get("input"), context=context),
            where=[dict(item) for item in list(expr.get("where", []) or [])],
        )
    if kind == "distribution":
        over = parse_semantic_expression(expr.get("over"), context=context)
        if not isinstance(over, EntityValueExpr):
            raise SemanticLayerError(
                "INVALID_EXPRESSION_AST", "distribution expressions require entity_value in 'over'"
            )
        dist_p = expr.get("p")
        return DistributionExpr(
            function=str(expr.get("function", "")).strip(),
            over=over,
            p=float(dist_p) if dist_p is not None else None,
            parameters=dict(expr.get("parameters", {}) or {}),
        )
    if kind == "conversion":
        entity = str(expr.get("entity", "")).strip()
        if not entity:
            raise SemanticLayerError(
                "CONVERSION_ENTITY_REQUIRED", "conversion expressions require 'entity'"
            )
        raw_window = expr.get("window", {}) or {}
        if not isinstance(raw_window, dict):
            raise SemanticLayerError(
                "CONVERSION_WINDOW_REQUIRED",
                "Conversion expressions require window as an object {unit, value}",
                details={
                    "expression_kind": "conversion",
                    "expression_position": context,
                    "path": "expression.window",
                    "received_type": type(raw_window).__name__,
                    "received_value": raw_window,
                    "recovery_hints": [
                        {
                            "code": "USE_OBJECT_SHAPE",
                            "message": (
                                "Wrap window as {unit, value}. E.g. window=7 "
                                "→ window={'unit': 'day', 'value': 7}."
                            ),
                            "suggested_shape": {
                                "unit": "<minute|hour|day|week|month|quarter|year>",
                                "value": "<positive integer>",
                            },
                        }
                    ],
                },
            )
        window = dict(raw_window)
        unit = str(window.get("unit", "")).strip()
        try:
            window_value = int(window.get("value", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise SemanticLayerError(
                "CONVERSION_WINDOW_REQUIRED",
                "conversion expressions require a positive window.value",
            ) from exc
        if not unit or window_value <= 0:
            raise SemanticLayerError(
                "CONVERSION_WINDOW_REQUIRED",
                "conversion expressions require a positive window.unit and window.value",
            )
        if unit not in _CONVERSION_WINDOW_UNITS:
            # An unrecognized unit compiles into DATE_DIFF('<unit>', ...)
            # and only fails at warehouse execution time. Reject here so
            # validate catches it.
            raise SemanticLayerError(
                "CONVERSION_WINDOW_REQUIRED",
                f"Unsupported conversion window.unit '{unit}'",
                details={
                    "path": "expression.window.unit",
                    "received_value": unit,
                    "supported_units": sorted(_CONVERSION_WINDOW_UNITS),
                },
            )
        matching_mode = str(expr.get("matching_mode", expr.get("matching", ""))).strip()
        if matching_mode not in {"first_converted_after_base", "closest_converted_after_base"}:
            raise SemanticLayerError(
                "CONVERSION_MATCHING_MODE_REQUIRED",
                "conversion expressions require a supported matching_mode",
            )
        dimension_bindings = {
            str(dim_id): {str(key): str(value) for key, value in dict(binding or {}).items()}
            for dim_id, binding in dict(expr.get("dimension_bindings", {}) or {}).items()
        }
        for dim_id, binding in dimension_bindings.items():
            unknown_binding_keys = sorted(set(binding) - {"side", "denominator"})
            if unknown_binding_keys:
                raise SemanticLayerError(
                    "INVALID_EXPRESSION_AST",
                    (
                        f"Unknown dimension_bindings keys {unknown_binding_keys} for "
                        f"'{dim_id}'; supported keys are 'side' and 'denominator'"
                    ),
                    details={"dimension": dim_id, "binding": binding},
                )
            side = binding.get("side", "")
            if side and side not in {"base", "converted"}:
                # An unrecognized side would silently behave as base-side.
                raise SemanticLayerError(
                    "INVALID_EXPRESSION_AST",
                    (
                        f"Unsupported dimension_bindings side '{side}' for '{dim_id}'; "
                        "use 'base' or 'converted'"
                    ),
                    details={"dimension": dim_id, "binding": binding},
                )
            denominator = binding.get("denominator", "")
            if denominator and denominator != "all_base_events":
                raise SemanticLayerError(
                    "INVALID_EXPRESSION_AST",
                    (
                        f"Unsupported dimension_bindings denominator '{denominator}' for "
                        f"'{dim_id}'; the supported denominator policy is 'all_base_events'"
                    ),
                    details={"dimension": dim_id, "binding": binding},
                )
        return ConversionExpr(
            base=parse_semantic_expression(expr.get("base"), context=context),
            converted=parse_semantic_expression(expr.get("converted"), context=context),
            entity=entity,
            window_unit=unit,
            window_value=window_value,
            matching_mode=matching_mode,
            constant_properties=[
                str(item) for item in list(expr.get("constant_properties", []) or [])
            ],
            dimension_bindings=dimension_bindings,
        )
    raise SemanticLayerError("INVALID_EXPRESSION_AST", f"Unsupported expression kind '{kind}'")


def collect_column_refs(expr: SemanticExpr) -> list[ColumnRefExpr]:
    refs: list[ColumnRefExpr] = []

    def _walk(node: SemanticExpr) -> None:
        if isinstance(node, ColumnRefExpr):
            refs.append(node)
            return
        if isinstance(
            node,
            (MeasureRefExpr, AggregateExpr, MetricRecipeRefExpr, LiteralExpr, ScopedAggregateExpr),
        ):
            return
        if isinstance(node, (ArithmeticExpr, ComparisonExpr)):
            _walk(node.left)
            _walk(node.right)
            return
        if isinstance(node, BooleanExpr):
            for arg in node.args:
                _walk(arg)
            return
        if isinstance(node, CallExpr):
            for arg in node.args:
                _walk(arg)
            return
        if isinstance(node, DateAddExpr):
            _walk(node.value)
            _walk(node.date)
            return
        if isinstance(node, InExpr):
            _walk(node.expr)
            for value in node.values:
                _walk(value)
            return
        if isinstance(node, CaseExpr):
            for item in node.whens:
                _walk(item.when)
                _walk(item.then)
            if node.else_expr is not None:
                _walk(node.else_expr)
            return
        if isinstance(node, CumulativeExpr):
            _walk(node.input)
            return
        if isinstance(node, RollingExpr):
            _walk(node.input)
            return
        if isinstance(node, PriorPeriodExpr):
            _walk(node.input)
            return
        if isinstance(node, PeriodToDateExpr):
            _walk(node.input)
            return
        if isinstance(node, OffsetWindowExpr):
            _walk(node.input)
            return
        if isinstance(node, MetricPredicateExpr):
            _walk(node.input)
            return
        if isinstance(node, RatioExpr):
            _walk(node.numerator)
            _walk(node.denominator)
            return
        if isinstance(node, EntityValueExpr):
            _walk(node.input)
            return
        if isinstance(node, DistributionExpr):
            _walk(node.over)
            return
        if isinstance(node, ConversionExpr):
            _walk(node.base)
            _walk(node.converted)
            return
        if isinstance(node, ConditionalAggregateExpr):
            _walk(node.condition)
            if node.value is not None:
                _walk(node.value)
            return
        raise TypeError(f"Unsupported expression node: {type(node)!r}")

    _walk(expr)
    return refs


def exprs_to_dict(rows: Iterable[SemanticExpr]) -> list[dict[str, Any]]:
    return [expr_to_dict(row) for row in rows]
