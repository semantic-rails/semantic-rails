"""Query IR — the normalized in-memory form of an inbound Query IR.

Exposes :class:`NormalizedQuery` and :func:`normalize_query` /
:func:`normalize_partial_query`. Every public entry point (validate,
compile, explain, query) normalizes its input here before the compiler
runs, so the rest of the runtime can rely on a single canonical shape
regardless of which surface (HTTP, MCP, CLI) the request came in on.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .errors import SemanticLayerError
from .expressions import (
    AggregateExpr,
    ArithmeticExpr,
    BooleanExpr,
    CallExpr,
    CaseExpr,
    ComparisonExpr,
    ConditionalAggregateExpr,
    ConversionExpr,
    CumulativeExpr,
    DistributionExpr,
    EntityValueExpr,
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
    expression_field,
    parse_semantic_expression,
)


@dataclass(frozen=True)
class QuerySelect:
    expression: SemanticExpr | None
    as_: str


@dataclass(frozen=True)
class Filter:
    field: str
    op: str
    value: Any


@dataclass(frozen=True)
class MetricFilter:
    expression: SemanticExpr | None
    op: str
    value: Any


@dataclass(frozen=True)
class TimeSpec:
    temporal_role: str
    grain: str = ""
    start: Any = None
    end: Any = None
    fill: bool = False
    calendar_id: str = "default"


@dataclass(frozen=True)
class OrderBy:
    field: str
    direction: str = "ASC"


@dataclass(frozen=True)
class PathPolicy:
    preference: str = "fewest_hops"
    ask_if_ambiguous: bool = True


@dataclass(frozen=True)
class NormalizedQuery:
    version: int
    select: list[QuerySelect]
    group_by: list[str] = field(default_factory=list)
    where: list[Filter] = field(default_factory=list)
    metric_filters: list[MetricFilter] = field(default_factory=list)
    time: TimeSpec | None = None
    temporal_role_overrides: dict[str, str] = field(default_factory=dict)
    path_policy: PathPolicy = field(default_factory=PathPolicy)
    order_by: list[OrderBy] = field(default_factory=list)
    limit: int | None = None
    debug: bool = False
    explain: bool = False
    export: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "select": [
                {
                    "expression": expr_to_dict(item.expression)
                    if item.expression is not None
                    else {},
                    "as": item.as_,
                }
                for item in self.select
            ],
            "group_by": list(self.group_by),
            "where": [asdict(item) for item in self.where],
            "metric_filters": [
                {
                    "expression": expr_to_dict(item.expression)
                    if item.expression is not None
                    else {},
                    "op": item.op,
                    "value": item.value,
                }
                for item in self.metric_filters
            ],
            "time": asdict(self.time) if self.time else None,
            "temporal_role_overrides": dict(self.temporal_role_overrides),
            "path_policy": asdict(self.path_policy),
            "order_by": [asdict(item) for item in self.order_by],
            "limit": self.limit,
            "debug": self.debug,
            "explain": self.explain,
            "export": self.export,
        }


@dataclass(frozen=True)
class PartialQueryState:
    version: int
    select: list[QuerySelect] = field(default_factory=list)
    group_by: list[str] = field(default_factory=list)
    where: list[Filter] = field(default_factory=list)
    metric_filters: list[MetricFilter] = field(default_factory=list)
    time: TimeSpec | None = None
    temporal_role_overrides: dict[str, str] = field(default_factory=dict)
    path_policy: PathPolicy = field(default_factory=PathPolicy)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "select": [
                {
                    "expression": expr_to_dict(item.expression)
                    if item.expression is not None
                    else {},
                    "as": item.as_,
                }
                for item in self.select
            ],
            "group_by": list(self.group_by),
            "where": [asdict(item) for item in self.where],
            "metric_filters": [
                {
                    "expression": expr_to_dict(item.expression)
                    if item.expression is not None
                    else {},
                    "op": item.op,
                    "value": item.value,
                }
                for item in self.metric_filters
            ],
            "time": asdict(self.time) if self.time else None,
            "temporal_role_overrides": dict(self.temporal_role_overrides),
            "path_policy": asdict(self.path_policy),
        }


def _validate_query_expr(expr: SemanticExpr) -> None:
    if isinstance(
        expr, (MeasureRefExpr, AggregateExpr, MetricRecipeRefExpr, LiteralExpr, ScopedAggregateExpr)
    ):
        return
    if isinstance(expr, RatioExpr):
        _validate_query_expr(expr.numerator)
        _validate_query_expr(expr.denominator)
        return
    if isinstance(expr, EntityValueExpr):
        _validate_query_expr(expr.input)
        return
    if isinstance(expr, DistributionExpr):
        _validate_query_expr(expr.over)
        return
    if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
        _validate_query_expr(expr.left)
        _validate_query_expr(expr.right)
        return
    if isinstance(expr, BooleanExpr):
        for arg in expr.args:
            _validate_query_expr(arg)
        return
    if isinstance(expr, CallExpr):
        for arg in expr.args:
            _validate_query_expr(arg)
        return
    if isinstance(expr, CaseExpr):
        for item in expr.whens:
            _validate_query_expr(item.when)
            _validate_query_expr(item.then)
        if expr.else_expr is not None:
            _validate_query_expr(expr.else_expr)
        return
    if isinstance(
        expr, (CumulativeExpr, RollingExpr, PriorPeriodExpr, PeriodToDateExpr, OffsetWindowExpr)
    ):
        _validate_query_expr(expr.input)
        return
    if isinstance(expr, MetricPredicateExpr):
        _validate_query_expr(expr.input)
        return
    if isinstance(expr, ConversionExpr):
        _validate_query_expr(expr.base)
        _validate_query_expr(expr.converted)
        return
    if isinstance(expr, ConditionalAggregateExpr):
        # The aggregate_if binding-time rewrite (see
        # ``compiler_parts/bind.py:lift_conditional_aggregates``) replaces
        # this node with an ``AggregateExpr`` before lowering. We do NOT
        # recurse into ``condition`` / ``value`` because they routinely
        # carry ``ColumnRefExpr`` nodes that ``_validate_query_expr``
        # rejects at the query level (column refs are otherwise only
        # legal inside measure-expression definitions at config level).
        # The rewrite phase validates the inner subtrees via
        # ``_conditional_aggregate_entity``.
        return
    raise SemanticLayerError(
        "INVALID_QUERY", f"Unsupported query expression kind '{expr_to_dict(expr)['kind']}'"
    )


_SUPPORTED_TIME_KEYS = {"temporal_role", "grain", "start", "end", "fill", "calendar_id", "range"}
_SUPPORTED_RELATIVE_UNITS = {"day", "week", "month", "quarter", "year"}


def _parse_now(policy_context: dict[str, Any] | None) -> date:
    raw_now = (policy_context or {}).get("now")
    if raw_now in (None, ""):
        return datetime.now(UTC).date()
    if isinstance(raw_now, datetime):
        return raw_now.date()
    if isinstance(raw_now, date):
        return raw_now
    text = str(raw_now).strip()
    if not text:
        return datetime.now(UTC).date()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise SemanticLayerError(
                "INVALID_QUERY",
                "policy_context.now must be an ISO date or datetime",
                details={"path": "policy_context.now", "value": text},
            ) from exc


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def _floor_period(value: date, unit: str) -> date:
    if unit == "day":
        return value
    if unit == "week":
        return value - timedelta(days=value.weekday())
    if unit == "month":
        return date(value.year, value.month, 1)
    if unit == "quarter":
        quarter_month = ((value.month - 1) // 3) * 3 + 1
        return date(value.year, quarter_month, 1)
    if unit == "year":
        return date(value.year, 1, 1)
    raise SemanticLayerError(
        "INVALID_QUERY",
        f"Unsupported relative time unit '{unit}'",
        details={
            "path": "time.range.last.unit",
            "supported_units": sorted(_SUPPORTED_RELATIVE_UNITS),
        },
    )


def _shift_period(value: date, unit: str, periods: int) -> date:
    if unit == "day":
        return value + timedelta(days=periods)
    if unit == "week":
        return value + timedelta(weeks=periods)
    if unit == "month":
        return _add_months(value, periods)
    if unit == "quarter":
        return _add_months(value, periods * 3)
    if unit == "year":
        return _add_months(value, periods * 12)
    raise SemanticLayerError(
        "INVALID_QUERY",
        f"Unsupported relative time unit '{unit}'",
        details={
            "path": "time.range.last.unit",
            "supported_units": sorted(_SUPPORTED_RELATIVE_UNITS),
        },
    )


def _relative_range_bounds(
    range_payload: Any, *, policy_context: dict[str, Any] | None
) -> dict[str, str]:
    if not isinstance(range_payload, dict):
        raise SemanticLayerError(
            "INVALID_QUERY", "query.time.range must be an object", details={"path": "time.range"}
        )
    unknown_keys = sorted(set(range_payload) - {"last"})
    if unknown_keys:
        # If the agent put `start` / `end` inside `range`, point at the
        # top-level `time.start` / `time.end` directly — the most common
        # confusion (blind-agent benchmark Q1).
        misplaced_bounds = sorted({"start", "end"} & set(unknown_keys))
        hints: list[dict[str, Any]] = []
        if misplaced_bounds:
            hints.append(
                {
                    "code": "MOVE_BOUNDS_TO_TIME_TOP_LEVEL",
                    "message": (
                        "Absolute date bounds live on `time.start` / "
                        "`time.end` directly — NOT inside `time.range`. "
                        "`time.range` is only for the relative form "
                        "`{last: {unit, value}}`."
                    ),
                    "suggested_query_ir_change": {
                        "move": {f"time.range.{key}": f"time.{key}" for key in misplaced_bounds}
                    },
                }
            )
        raise SemanticLayerError(
            "INVALID_QUERY",
            "query.time.range contains unsupported keys",
            details={
                "path": "time.range",
                "unsupported_keys": unknown_keys,
                "supported_keys": ["last"],
                "recovery_hints": hints,
            },
        )
    last = range_payload.get("last")
    if not isinstance(last, dict):
        raise SemanticLayerError(
            "INVALID_QUERY",
            "query.time.range.last must be an object with unit and value",
            details={
                "path": "time.range.last",
                "received_type": type(last).__name__,
                "received_value": last,
                "recovery_hints": [
                    {
                        "code": "USE_OBJECT_SHAPE",
                        "message": (
                            "Replace the string shorthand with a {unit, value} object. "
                            "E.g. '90 days' → {'unit': 'day', 'value': 90}, "
                            "'3 months' → {'unit': 'month', 'value': 3}."
                        ),
                        "suggested_query_ir_change": {
                            "path": "time.range.last",
                            "shape": {
                                "unit": "<day|week|month|quarter|year>",
                                "value": "<positive integer>",
                            },
                        },
                    },
                ],
            },
        )
    unknown_last_keys = sorted(set(last) - {"unit", "value"})
    if unknown_last_keys:
        raise SemanticLayerError(
            "INVALID_QUERY",
            "query.time.range.last contains unsupported keys",
            details={
                "path": "time.range.last",
                "unsupported_keys": unknown_last_keys,
                "supported_keys": ["unit", "value"],
            },
        )
    unit = str(last.get("unit", "") or "").strip().lower()
    if unit not in _SUPPORTED_RELATIVE_UNITS:
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"Unsupported relative time unit '{unit}'",
            details={
                "path": "time.range.last.unit",
                "supported_units": sorted(_SUPPORTED_RELATIVE_UNITS),
            },
        )
    try:
        value = int(last.get("value"))  # type: ignore[arg-type]  # None raises TypeError, caught below
    except (TypeError, ValueError) as exc:
        raise SemanticLayerError(
            "INVALID_QUERY",
            "query.time.range.last.value must be a positive integer",
            details={"path": "time.range.last.value"},
        ) from exc
    if value <= 0:
        raise SemanticLayerError(
            "INVALID_QUERY",
            "query.time.range.last.value must be a positive integer",
            details={"path": "time.range.last.value", "value": value},
        )
    end = _floor_period(_parse_now(policy_context), unit)
    start = _shift_period(end, unit, -value)
    return {"start": start.isoformat(), "end": end.isoformat()}


def _time_spec_from_payload(
    payload: Any,
    *,
    allow_missing_temporal_role: bool = False,
    policy_context: dict[str, Any] | None = None,
) -> TimeSpec | None:
    if not payload:
        return None
    if not isinstance(payload, dict):
        raise SemanticLayerError("INVALID_QUERY", "query.time must be an object")
    unknown_keys = sorted(set(payload) - _SUPPORTED_TIME_KEYS)
    if unknown_keys:
        raise SemanticLayerError(
            "INVALID_QUERY",
            "query.time contains unsupported keys",
            details={
                "path": "time",
                "unsupported_keys": unknown_keys,
                "supported_keys": sorted(_SUPPORTED_TIME_KEYS),
            },
        )
    temporal_role = str(payload.get("temporal_role", "")).strip()
    if not allow_missing_temporal_role and not temporal_role:
        raise SemanticLayerError("INVALID_QUERY", "query.time.temporal_role is required")
    expanded = {}
    if "range" in payload:
        if "start" in payload or "end" in payload:
            raise SemanticLayerError(
                "INVALID_QUERY",
                "query.time.range cannot be combined with query.time.start or query.time.end",
                details={"path": "time.range"},
            )
        expanded = _relative_range_bounds(payload.get("range"), policy_context=policy_context)
    return TimeSpec(
        temporal_role=temporal_role,
        grain=str(payload.get("grain", "")),
        start=expanded.get("start", payload.get("start")),
        end=expanded.get("end", payload.get("end")),
        fill=bool(payload.get("fill", False)),
        calendar_id=str(payload.get("calendar_id", "default") or "default"),
    )


def _filter_from_payload(item: Any, index: int) -> Filter:
    if not isinstance(item, dict):
        raise SemanticLayerError(
            "INVALID_EXPRESSION_AST",
            f"where[{index}] must be an object",
            details={
                "path": f"where[{index}]",
                "why_invalid": "where filters require field/op/value keys",
            },
        )
    field = str(item.get("field", "") or "").strip()
    if not field:
        raise SemanticLayerError(
            "INVALID_EXPRESSION_AST",
            f"where[{index}].field is required",
            details={
                "path": f"where[{index}].field",
                "why_invalid": (
                    "where filters target a dimension by id; the key is ``field`` "
                    "(same vocabulary as ``order_by[].field``). Expression filters "
                    "belong in ``metric_filters``."
                ),
                "suggested_query_ir_change": (
                    "Use ``field: 'dimension.<id>'`` on where items; move "
                    "expression filters to metric_filters."
                ),
            },
        )
    value = item.get("value")
    op = " ".join(str(item.get("op", "=")).upper().split())
    # Reject dict values in ``where[]`` — they would silently stringify
    # to ``"{'kind': 'percentile', ...}"`` via SqlLiteral and produce a
    # broken comparison. The metric_predicate.value path supports
    # inline percentile thresholds (round-two Phase 2); ``where`` does
    # not. Tell the agent the right slot to use.
    if isinstance(value, dict):
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"where[{index}].value must be a scalar (or list for IN), not an object.",
            details={
                "path": f"where[{index}].value",
                "received_kind": str(value.get("kind", "")),
                "why_invalid": (
                    "where filters compare a dimension to a literal. "
                    "Inline expression thresholds (e.g. {kind: 'percentile'}) "
                    "belong in metric_predicate.value inside a "
                    "scoped_aggregate predicate or a metric_filters entry."
                ),
                "suggested_query_ir_change": (
                    "Compute the threshold value in a separate query and "
                    "pass the scalar result here, OR move this filter to "
                    "metric_filters[].expression as a metric_predicate."
                ),
            },
        )
    if op in {"IN", "NOT IN"}:
        if value is None:
            raise SemanticLayerError(
                "INVALID_QUERY",
                f"where[{index}].value is required for op '{op}'",
                details={
                    "path": f"where[{index}].value",
                    "op": op,
                    "why_invalid": f"'{op}' compares a dimension against a list of scalars.",
                    "recovery_hints": [
                        {
                            "code": "USE_LIST_VALUE_OR_NULL_TEST",
                            "message": (
                                f"Pass value: [..] for '{op}'. To test for NULL, "
                                "use op 'IS NULL' / 'IS NOT NULL' instead."
                            ),
                        }
                    ],
                },
            )
        # A bare scalar for IN / NOT IN unambiguously means a one-element
        # list — wrap it here. Without this, the lowering's
        # ``list(value)`` character-split strings ('Philadelphia' became
        # IN ('P', 'h', 'i', ...)) and silently matched the wrong rows.
        if not isinstance(value, list):
            value = [value]
        for vi, v in enumerate(value):
            if isinstance(v, dict):
                raise SemanticLayerError(
                    "INVALID_QUERY",
                    f"where[{index}].value[{vi}] must be a scalar, not an object.",
                    details={
                        "path": f"where[{index}].value[{vi}]",
                        "why_invalid": "IN-list elements must be literal scalars.",
                    },
                )
    return Filter(field=field, op=str(item.get("op", "=")), value=value)


def _require_metric_filter_object(item: Any, index: int) -> dict[str, Any]:
    """Reject non-dict ``metric_filters[]`` entries with a structured error.

    Without this, ``item.get(...)`` on a string/list entry raised a bare
    ``AttributeError`` that surfaced as INTERNAL_ERROR at the MCP/HTTP
    boundary instead of a recoverable INVALID_QUERY.
    """
    if not isinstance(item, dict):
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"metric_filters[{index}] must be an object",
            details={
                "path": f"metric_filters[{index}]",
                "received_type": type(item).__name__,
                "why_invalid": (
                    "metric_filters entries are objects shaped {expression, op, value}."
                ),
                "recovery_hints": [
                    {
                        "code": "USE_OBJECT_SHAPE",
                        "message": (
                            "Each metric_filters item is an object: "
                            "{'expression': {...}, 'op': '<comparison op>', "
                            "'value': <scalar>}."
                        ),
                    }
                ],
            },
        )
    return item


_PATH_POLICY_KEYS = ("preference", "ask_if_ambiguous")


def _path_policy_from_payload(raw: Any) -> PathPolicy:
    """Build a :class:`PathPolicy` from the raw payload with type guards.

    ``PathPolicy(**payload["path_policy"])`` raised a bare ``TypeError``
    on non-dict payloads or unexpected keys, surfacing as INTERNAL_ERROR
    instead of a recoverable INVALID_QUERY.
    """
    if not raw:
        return PathPolicy()
    if not isinstance(raw, dict):
        raise SemanticLayerError(
            "INVALID_QUERY",
            "query.path_policy must be an object",
            details={
                "path": "path_policy",
                "received_type": type(raw).__name__,
                "supported_keys": list(_PATH_POLICY_KEYS),
            },
        )
    unknown_keys = sorted(set(raw) - set(_PATH_POLICY_KEYS))
    if unknown_keys:
        raise SemanticLayerError(
            "INVALID_QUERY",
            "query.path_policy contains unsupported keys",
            details={
                "path": "path_policy",
                "unsupported_keys": unknown_keys,
                "supported_keys": list(_PATH_POLICY_KEYS),
                "recovery_hints": [
                    {
                        "code": "REMOVE_UNKNOWN_KEY",
                        "message": (
                            "path_policy accepts only 'preference' and 'ask_if_ambiguous'."
                        ),
                        "suggested_query_ir_change": {
                            "remove": [f"path_policy.{key}" for key in unknown_keys]
                        },
                    }
                ],
            },
        )
    return PathPolicy(
        preference=str(raw.get("preference", "fewest_hops") or "fewest_hops"),
        ask_if_ambiguous=bool(raw.get("ask_if_ambiguous", True)),
    )


def _time_output_alias(time: TimeSpec | None) -> str:
    if time is None or not time.temporal_role:
        return ""
    return time.temporal_role if not time.grain else f"{time.temporal_role}__{time.grain}"


def _assert_unique_output_aliases(
    select: list[QuerySelect], group_by: list[str], time: TimeSpec | None
) -> None:
    alias_sources: dict[str, list[str]] = {}
    for index, dim_id in enumerate(group_by):
        alias_sources.setdefault(dim_id, []).append(f"group_by[{index}]")
    time_alias = _time_output_alias(time)
    if time_alias:
        alias_sources.setdefault(time_alias, []).append("time")
    for index, item in enumerate(select):
        alias_sources.setdefault(item.as_, []).append(f"select[{index}].as")

    duplicates = {alias: sources for alias, sources in alias_sources.items() if len(sources) > 1}
    if duplicates:
        duplicate_alias = next(iter(duplicates))
        raise SemanticLayerError(
            "DUPLICATE_OUTPUT_ALIAS",
            f"Duplicate projected output alias '{duplicate_alias}'",
            details={"alias": duplicate_alias, "duplicates": duplicates},
        )


_VALID_QUERY_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        # Canonical IR keys consumed by normalize_query.
        "version",
        "select",
        "group_by",
        "where",
        "metric_filters",
        "order_by",
        "limit",
        "time",
        "temporal_role_overrides",
        "path_policy",
        "debug",
        "explain",
        "export",
        # Envelope keys consumed by the runtime / MCP layer (verbosity,
        # sql_profile, policy_context, limits, request_id). They reach
        # normalize_query in the payload but are not part of the IR
        # contract — accept them silently rather than rejecting.
        "policy_context",
        "limits",
        "verbosity",
        "sql_profile",
        "request_id",
    }
)


# Common typos / wrong-position keys that have a canonical home. Keep this
# narrow — only entries where the right answer is unambiguous. Each value
# is (canonical_key, one_line_explanation).
_TOP_LEVEL_KEY_TYPOS: dict[str, tuple[str, str]] = {
    "filter": (
        "where",
        "Dimension filters live at top-level `where[]` as "
        "{field, op, value}. Expression filters live in "
        "`metric_filters[]`.",
    ),
    "filters": (
        "where",
        "Dimension filters live at top-level `where[]` as "
        "{field, op, value}. Expression filters live in "
        "`metric_filters[]`.",
    ),
    "dimensions": (
        "group_by",
        "Dimensions to group by are bare strings on `group_by[]` — not objects.",
    ),
    "measures": (
        "select",
        "Measures are projected via `select[]` items shaped "
        "{expression: {aggregation, measure}, as: '<alias>'}.",
    ),
    "having": (
        "metric_filters",
        "Post-aggregation predicates live in `metric_filters[]` — "
        "the IR has no top-level `having` key.",
    ),
    "orderby": ("order_by", "Use `order_by[]` (snake_case)."),
    "groupby": ("group_by", "Use `group_by[]` (snake_case)."),
}


_SUPPORTED_QUERY_IR_VERSIONS: frozenset[int] = frozenset({1, 2})


def _check_supported_version(payload: dict[str, Any]) -> None:
    raw_version = payload.get("version", 1)
    try:
        version = int(raw_version)
    except (TypeError, ValueError) as exc:
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"Query IR version must be an integer in {sorted(_SUPPORTED_QUERY_IR_VERSIONS)}, got {raw_version!r}",
            details={"path": "version", "version": raw_version},
        ) from exc
    if version not in _SUPPORTED_QUERY_IR_VERSIONS:
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"Unsupported Query IR version {version}; supported versions are {sorted(_SUPPORTED_QUERY_IR_VERSIONS)}",
            details={
                "path": "version",
                "version": version,
                "supported_versions": sorted(_SUPPORTED_QUERY_IR_VERSIONS),
            },
        )


def _check_unknown_top_level_keys(payload: dict[str, Any]) -> None:
    """Reject unrecognized top-level keys before silently dropping them.

    Without this, payloads like `{select, time, filters: [...]}` parsed
    cleanly (the runtime ignored `filters`) and the agent thought the
    filter applied — a silent semantic drift the blind-agent benchmark
    flagged as the most dangerous failure mode.
    """
    # Treat underscore-prefixed keys (e.g. `_note`, `_comment`) as
    # documentation metadata — example fixtures use them inline to
    # explain shape. They never reach the SQL builder.
    unknown = sorted(
        key for key in set(payload) - _VALID_QUERY_TOP_LEVEL_KEYS if not key.startswith("_")
    )
    if not unknown:
        return
    bad_key = unknown[0]
    suggestion = _TOP_LEVEL_KEY_TYPOS.get(bad_key)
    hint: dict[str, Any] = {
        "code": "USE_CANONICAL_KEY" if suggestion else "REMOVE_UNKNOWN_KEY",
    }
    if suggestion:
        canonical, why = suggestion
        hint["message"] = (
            f"Top-level key '{bad_key}' is not a Query IR key. Use '{canonical}' instead. {why}"
        )
        hint["suggested_query_ir_change"] = {
            "rename": {bad_key: canonical},
        }
    else:
        hint["message"] = (
            f"Top-level key '{bad_key}' is not a recognized Query IR key. "
            f"Valid top-level keys: {sorted(_VALID_QUERY_TOP_LEVEL_KEYS)}."
        )
        hint["suggested_query_ir_change"] = {"remove": [bad_key]}
    raise SemanticLayerError(
        "INVALID_QUERY",
        f"Query payload contains unsupported top-level key(s): {unknown}",
        details={
            "path": bad_key,
            "unsupported_keys": unknown,
            "supported_keys": sorted(_VALID_QUERY_TOP_LEVEL_KEYS),
            "recovery_hints": [hint],
        },
    )


def _normalize_group_by_list(raw_items: Any) -> list[str]:
    """Convert a raw ``group_by`` payload entry list to bare dim-id strings.

    ``group_by`` accepts strings only; callers occasionally pass the
    ``{"dimension": "<id>"}`` select[] shorthand by mistake. Unwrap that
    here so the downstream resolver sees a clean id — otherwise the
    error path stringifies the dict into the message:
    ``Unknown dimension '{'dimension': 'dimension.fake_dim'}'``
    """
    raw = list(raw_items or [])
    out: list[str] = []
    for index, item in enumerate(raw):
        if isinstance(item, dict):
            for key in ("dimension", "field"):
                if key in item:
                    out.append(str(item.get(key, "")).strip())
                    break
            else:
                raise SemanticLayerError(
                    "INVALID_QUERY",
                    (
                        f"group_by[{index}] must be a bare dimension id string "
                        "(not an object). Use group_by: ['dimension.<id>']."
                    ),
                    details={"path": f"group_by[{index}]", "received_type": "dict"},
                )
            continue
        out.append(str(item))
    return out


def normalize_query(payload: dict[str, Any]) -> NormalizedQuery:
    _check_unknown_top_level_keys(payload)
    _check_supported_version(payload)
    # Validate `select` is a list before iterating. ``list(scalar)`` either
    # raises TypeError (int, float, bool) which leaks through the MCP
    # boundary as INTERNAL_ERROR, or silently yields character-wise
    # iteration (string) which produces a confusing
    # ``select[0] must be an object`` error pointing at the first char.
    raw_select = payload.get("select", [])
    if raw_select is not None and not isinstance(raw_select, list):
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"query.select must be a list of select items; got {type(raw_select).__name__}",
            details={"path": "select", "received_type": type(raw_select).__name__},
        )
    select_rows = list(raw_select or [])
    select: list[QuerySelect] = []
    # Dimension ids passed via ``select[]`` as the shorthand
    # ``{"dimension": "<id>"}`` shape get promoted to ``group_by[]`` so
    # the same reachability / path validation fires (otherwise a dim
    # bound to an unreachable entity would be silently dropped from
    # the SQL output).
    promoted_group_by: list[str] = []
    for idx, row in enumerate(select_rows):
        if not isinstance(row, dict):
            raise SemanticLayerError("INVALID_QUERY", f"select[{idx}] must be an object")
        if "dimension" in row and "expression" not in row:
            dim_id = str(row.get("dimension", "") or "").strip()
            if not dim_id:
                raise SemanticLayerError(
                    "INVALID_QUERY",
                    f"select[{idx}].dimension must be a non-empty dimension id",
                )
            promoted_group_by.append(dim_id)
            continue
        expression = parse_semantic_expression(row.get("expression", {}) or {}, context="query")
        _validate_query_expr(expression)
        if isinstance(expression, MetricPredicateExpr):
            raise SemanticLayerError(
                "INVALID_METRIC_PREDICATE",
                "metric_predicate expressions cannot be selected directly",
            )
        alias = str(row.get("as", "")).strip()
        if not alias:
            if isinstance(expression, MeasureRefExpr):
                alias = str(expression.measure).split(".")[-1]
            elif isinstance(expression, MetricRecipeRefExpr):
                alias = str(expression.metric_recipe).split(".")[-1]
            else:
                alias = f"expr_{idx + 1}"
        select.append(QuerySelect(expression=expression, as_=alias))
    where = [
        _filter_from_payload(item, idx)
        for idx, item in enumerate(list(payload.get("where", []) or []))
    ]
    metric_filters: list[MetricFilter] = []
    for mf_idx, item in enumerate(list(payload.get("metric_filters", []) or [])):
        item = _require_metric_filter_object(item, mf_idx)
        mf_value = item.get("value")
        # Outer metric_filters[].value is the right-hand-side scalar
        # the planner compares the expression to. Dict values here
        # would silently stringify (same footgun as ``where[].value``).
        # The inner ``expression.value`` (for metric_predicate) keeps
        # its widened shape from round-two Phase 2.
        if isinstance(mf_value, dict):
            raise SemanticLayerError(
                "INVALID_QUERY",
                f"metric_filters[{mf_idx}].value must be a scalar (or list for IN), not an object.",
                details={
                    "path": f"metric_filters[{mf_idx}].value",
                    "received_kind": str(mf_value.get("kind", "")),
                    "why_invalid": (
                        "metric_filters carry a scalar right-hand-side. "
                        "Inline percentile thresholds live under "
                        "expression.value (inside a metric_predicate) — "
                        "not at the outer metric_filters envelope level."
                    ),
                },
            )
        metric_filters.append(
            MetricFilter(
                expression=parse_semantic_expression(
                    item.get("expression", {}) or {}, context="query"
                ),
                op=str(item.get("op", "=")),
                value=mf_value,
            )
        )
    for item in metric_filters:
        assert (
            item.expression is not None
        )  # parse_semantic_expression returns non-None for non-empty payload
        _validate_query_expr(item.expression)
    group_by = _normalize_group_by_list(payload.get("group_by", []))
    # Append dim ids promoted from select[].dimension shorthand,
    # de-duplicating against any already-present group_by entries.
    seen_group = set(group_by)
    for dim_id in promoted_group_by:
        if dim_id not in seen_group:
            group_by.append(dim_id)
            seen_group.add(dim_id)
    time = _time_spec_from_payload(
        payload.get("time"), policy_context=dict(payload.get("policy_context", {}) or {})
    )
    if not select and not group_by and time is None:
        raise SemanticLayerError(
            "INVALID_QUERY", "Query requires select expressions, group_by, or time"
        )
    order_by = [
        OrderBy(
            field=str(expression_field(item, "field", expression_position="order_by")),
            # ``expression_field`` rejects non-dict entries first, so this
            # ``.get`` call is safe.
            direction=str(item.get("direction", "ASC")).upper(),
        )
        for item in list(payload.get("order_by", []) or [])
    ]
    _assert_unique_output_aliases(select, group_by, time)
    # order_by ghost-alias guard: every field must resolve to a select alias,
    # a group_by dimension ID, or the special "time" axis. Without this, a
    # typo in `order_by[].field` passes validate then errors at the warehouse
    # with a raw "column not found" message.
    select_aliases = {item.as_ for item in select}
    group_by_ids = set(group_by)
    time_role_alias = ""
    if time is not None:
        time_role_alias = (
            f"{time.temporal_role}__{time.grain}" if time.grain else time.temporal_role
        )
    for entry in order_by:
        if entry.field == "time":
            if time is None:
                raise SemanticLayerError(
                    "INVALID_ORDER_BY",
                    "order_by 'time' requires a query time axis",
                )
            continue
        if (
            entry.field in select_aliases
            or entry.field in group_by_ids
            or entry.field == time_role_alias
        ):
            continue
        raise SemanticLayerError(
            "INVALID_ORDER_BY",
            f"order_by field {entry.field!r} does not resolve to a select alias, group_by dimension, or the time axis",
            details={
                "field": entry.field,
                "available_select_aliases": sorted(select_aliases),
                "available_group_by": sorted(group_by_ids),
                "available_time_axis": time_role_alias,
            },
        )
    raw_limit = payload.get("limit")
    limit_value: int | None = None
    if raw_limit is not None:
        try:
            limit_value = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise SemanticLayerError(
                "INVALID_QUERY",
                f"limit must be a non-negative integer, got {raw_limit!r}",
                details={"limit": raw_limit},
            ) from exc
        if limit_value < 0:
            raise SemanticLayerError(
                "INVALID_QUERY",
                f"limit must be a non-negative integer, got {limit_value}",
                details={"limit": limit_value},
            )
    return NormalizedQuery(
        version=int(payload.get("version", 1)),
        select=select,
        group_by=group_by,
        where=where,
        metric_filters=metric_filters,
        time=time,
        temporal_role_overrides={
            str(k): str(v)
            for k, v in dict(payload.get("temporal_role_overrides", {}) or {}).items()
        },
        path_policy=_path_policy_from_payload(payload.get("path_policy")),
        order_by=order_by,
        limit=limit_value,
        debug=bool(payload.get("debug", False)),
        explain=bool(payload.get("explain", False)),
        export=bool(payload.get("export", False)),
    )


def normalize_partial_query(payload: dict[str, Any]) -> PartialQueryState:
    # Same silent-drift defense as normalize_query — build_options /
    # plan also accept partial query payloads and should reject
    # unknown top-level keys with the same USE_CANONICAL_KEY hint.
    _check_unknown_top_level_keys(payload)
    _check_supported_version(payload)
    select: list[QuerySelect] = []
    for idx, row in enumerate(list(payload.get("select", []) or [])):
        if not isinstance(row, dict):
            raise SemanticLayerError("INVALID_QUERY", f"select[{idx}] must be an object")
        raw_expr = row.get("expression", {}) or {}
        expression = parse_semantic_expression(raw_expr, context="query") if raw_expr else None
        if expression:
            _validate_query_expr(expression)
            if isinstance(expression, MetricPredicateExpr):
                raise SemanticLayerError(
                    "INVALID_METRIC_PREDICATE",
                    "metric_predicate expressions cannot be selected directly",
                )
        alias = str(row.get("as", "")).strip() or f"expr_{idx + 1}"
        select.append(QuerySelect(expression=expression, as_=alias))
    where = [
        _filter_from_payload(item, idx)
        for idx, item in enumerate(list(payload.get("where", []) or []))
    ]
    metric_filters = []
    for mf_idx, item in enumerate(list(payload.get("metric_filters", []) or [])):
        item = _require_metric_filter_object(item, mf_idx)
        metric_filters.append(
            MetricFilter(
                expression=parse_semantic_expression(
                    item.get("expression", {}) or {}, context="query"
                )
                if item.get("expression")
                else None,
                op=str(item.get("op", "=")),
                value=item.get("value"),
            )
        )
    for item in metric_filters:
        if item.expression is not None:
            _validate_query_expr(item.expression)
    group_by = _normalize_group_by_list(payload.get("group_by", []))
    time = _time_spec_from_payload(
        payload.get("time"),
        allow_missing_temporal_role=True,
        policy_context=dict(payload.get("policy_context", {}) or {}),
    )
    _assert_unique_output_aliases(select, group_by, time if time and time.temporal_role else None)
    return PartialQueryState(
        version=int(payload.get("version", 1)),
        select=select,
        group_by=group_by,
        where=where,
        metric_filters=metric_filters,
        time=time,
        temporal_role_overrides={
            str(k): str(v)
            for k, v in dict(payload.get("temporal_role_overrides", {}) or {}).items()
        },
        path_policy=_path_policy_from_payload(payload.get("path_policy")),
    )
