"""Query IR — the normalized in-memory form of an inbound Query IR.

Exposes :class:`NormalizedQuery` and :func:`normalize_query` /
:func:`normalize_partial_query`. Every public entry point (validate,
compile, explain, query) normalizes its input here before the compiler
runs, so the rest of the runtime can rely on a single canonical shape
regardless of which surface (HTTP, MCP, CLI) the request came in on.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

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
from .schema import OBSERVATION_SCOPES, PackageConfig


@dataclass(frozen=True)
class QuerySelect:
    expression: SemanticExpr | None
    as_: str


@dataclass(frozen=True)
class Filter:
    field: str
    op: str
    value: Any


CHILD_GROUP_MATCHES = ("any", "none")


@dataclass(frozen=True)
class ChildGroup:
    """Conditions that one row of a child entity meets together.

    ``any`` keeps a row of the measure's entity when at least one of its child rows meets every
    condition; ``none`` keeps it when none does. It is a ``where`` item of its own, so a reader
    of ``where`` applies it or refuses it; it is never read as a plain filter.
    """

    child: str
    match: str
    where: list[Filter]


WhereItem = Filter | ChildGroup


def is_child_group(item: Any) -> bool:
    """A child group, normalized or in its payload form."""
    return isinstance(item, ChildGroup) or (isinstance(item, dict) and "child" in item)


def child_groups(items: Iterable[Any] | None) -> list[Any]:
    """The child groups of a ``where`` list, in either form."""
    return [item for item in items or [] if is_child_group(item)]


def plain_filters(items: Iterable[Any] | None) -> list[Any]:
    """The filters on the query's own rows: every ``where`` item but the child groups.

    Only for a reader that applies the groups itself or for which a group is no filter on
    the output rows (a required filter, a pinned value).
    """
    return [item for item in items or [] if not is_child_group(item)]


def every_filter(items: Iterable[Any] | None) -> list[Any]:
    """Every filter of a ``where`` list, a child group's conditions included: the dimensions a
    query reads, for access checks and bindings."""
    out: list[Any] = []
    for item in items or []:
        if isinstance(item, ChildGroup):
            out.extend(item.where)
        elif is_child_group(item):
            out.extend(list(item.get("where") or []))
        else:
            out.append(item)
    return out


def refuse_child_groups(items: Iterable[Any] | None, reader: str) -> list[Any]:
    """The ``where`` items, when none is a child group; ``reader`` cannot apply one."""
    rows = list(items or [])
    groups = child_groups(rows)
    if groups:
        group = groups[0]
        child = group.child if isinstance(group, ChildGroup) else str(group.get("child", ""))
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"A child group in 'where' is not supported {reader}.",
            details={
                "path": f"where[{rows.index(group)}]",
                "child": child,
                "why_invalid": (
                    f"Child groups filter a measure's rows by their child rows; {reader} "
                    "they cannot be applied, and dropping one would answer a wider question."
                ),
            },
        )
    return rows


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
class RouteDecision:
    """A ``route_decisions`` row: this query's route for one exact (source, target) pair,
    shaped like a ``graph.path_preferences`` row (its ``label`` is ignored)."""

    source_entity: str
    target_entity: str
    relationship_path: list[str]


_ROUTE_DECISION_KEYS = frozenset({"source_entity", "target_entity", "relationship_path", "label"})


def route_decisions_from_payload(payload: dict[str, Any]) -> list[RouteDecision]:
    """The shape of ``route_decisions``; the package checks each row when the query binds."""
    raw = payload.get("route_decisions")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SemanticLayerError(
            "INVALID_QUERY",
            "route_decisions must be a list of rows shaped "
            "{source_entity, target_entity, relationship_path}",
            details={"path": "route_decisions", "received_type": type(raw).__name__},
        )
    rows: list[RouteDecision] = []
    for index, row in enumerate(raw):
        where = f"route_decisions[{index}]"
        path = row.get("relationship_path") if isinstance(row, dict) else None
        if (
            not isinstance(row, dict)
            or set(row) - _ROUTE_DECISION_KEYS
            or not all(
                isinstance(row.get(key), str) and row[key].strip()
                for key in ("source_entity", "target_entity")
            )
            or not isinstance(path, list)
            or not path
            or not all(isinstance(hop, str) and hop.strip() for hop in path)
        ):
            raise SemanticLayerError(
                "INVALID_QUERY",
                f"{where} must be an object with source_entity, target_entity and a non-empty "
                "relationship_path list (the decision of an AMBIGUOUS_PATH option)",
                details={
                    "path": where,
                    "reason": "malformed_route_decision",
                    "unsupported_keys": sorted(set(row) - _ROUTE_DECISION_KEYS)
                    if isinstance(row, dict)
                    else [],
                },
            )
        rows.append(
            RouteDecision(
                source_entity=row["source_entity"].strip(),
                target_entity=row["target_entity"].strip(),
                relationship_path=[hop.strip() for hop in path],
            )
        )
    return rows


@dataclass(frozen=True)
class NormalizedQuery:
    version: int
    select: list[QuerySelect]
    group_by: list[str] = field(default_factory=list)
    where: list[WhereItem] = field(default_factory=list)
    metric_filters: list[MetricFilter] = field(default_factory=list)
    time: TimeSpec | None = None
    temporal_role_overrides: dict[str, str] = field(default_factory=dict)
    order_by: list[OrderBy] = field(default_factory=list)
    limit: int | None = None
    debug: bool = False
    explain: bool = False
    export: bool = False
    route_decisions: list[RouteDecision] = field(default_factory=list)
    observation_scope: str = ""  # "" defers to the package's defaults.observation_scope

    def to_dict(self) -> dict[str, Any]:
        # Only a query that decides a route or a scope carries the key, so other queries'
        # normalized forms (and compile-cache keys) are unchanged.
        decisions = (
            {"route_decisions": [asdict(row) for row in self.route_decisions]}
            if self.route_decisions
            else {}
        )
        scope = {"observation_scope": self.observation_scope} if self.observation_scope else {}
        return {
            **decisions,
            **scope,
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
    where: list[WhereItem] = field(default_factory=list)
    metric_filters: list[MetricFilter] = field(default_factory=list)
    time: TimeSpec | None = None
    temporal_role_overrides: dict[str, str] = field(default_factory=dict)

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


def _parse_now(policy_context: dict[str, Any] | None) -> date | datetime:
    raw_now = (policy_context or {}).get("now")
    if raw_now in (None, ""):
        return datetime.now(UTC)
    if isinstance(raw_now, datetime):
        return raw_now
    if isinstance(raw_now, date):
        return raw_now
    text = str(raw_now).strip()
    if not text:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
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
    range_payload: Any,
    *,
    policy_context: dict[str, Any] | None,
    timezone: str = "UTC",
    calendar_id: str = "default",
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
    if calendar_id.strip().lower() not in {"", "default"} and unit != "day":
        raise SemanticLayerError(
            "INVALID_QUERY",
            "Relative periods coarser than day require the default calendar; "
            "use exact time.start and time.end dates for this calendar.",
            details={"path": "time.range.last.unit", "calendar_id": calendar_id, "unit": unit},
        )
    now = _parse_now(policy_context)
    if isinstance(now, datetime):
        if now.tzinfo is not None:
            now = now.astimezone(ZoneInfo(timezone or "UTC"))
        now = now.date()
    end = _floor_period(now, unit)
    start = _shift_period(end, unit, -value)
    return {"start": start.isoformat(), "end": end.isoformat()}


def _time_spec_from_payload(
    payload: Any,
    *,
    allow_missing_temporal_role: bool = False,
    policy_context: dict[str, Any] | None = None,
    config: PackageConfig | None = None,
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
        timezone = "UTC"
        calendar_id = str(payload.get("calendar_id", "default") or "default")
        if config is not None and temporal_role:
            role = next((r for r in config.temporal_roles if r.id == temporal_role), None)
            if role is None:
                raise SemanticLayerError(
                    "INVALID_TEMPORAL_ROLE", f"Unknown temporal role '{temporal_role}'"
                )
            timezone = role.timezone or "UTC"
            dimension = next(d for d in config.dimensions if d.id == role.dimension)
            entity = next(e for e in config.entities if e.id == dimension.entity)
            if calendar_id.strip().lower() in {"", "default"}:
                calendar_id = entity.calendar_id or "default"
        expanded = _relative_range_bounds(
            payload.get("range"),
            policy_context=policy_context,
            timezone=timezone,
            calendar_id=calendar_id,
        )
    return TimeSpec(
        temporal_role=temporal_role,
        grain=str(payload.get("grain", "")),
        start=expanded.get("start", payload.get("start")),
        end=expanded.get("end", payload.get("end")),
        fill=bool(payload.get("fill", False)),
        calendar_id=str(payload.get("calendar_id", "default") or "default"),
    )


def _filter_from_payload(item: Any, path: str) -> Filter:
    if not isinstance(item, dict):
        raise SemanticLayerError(
            "INVALID_EXPRESSION_AST",
            f"{path} must be an object",
            details={
                "path": path,
                "why_invalid": "where filters require field/op/value keys",
            },
        )
    field = str(item.get("field", "") or "").strip()
    if not field:
        raise SemanticLayerError(
            "INVALID_EXPRESSION_AST",
            f"{path}.field is required",
            details={
                "path": f"{path}.field",
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
            f"{path}.value must be a scalar (or list for IN), not an object.",
            details={
                "path": f"{path}.value",
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
                f"{path}.value is required for op '{op}'",
                details={
                    "path": f"{path}.value",
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
                    f"{path}.value[{vi}] must be a scalar, not an object.",
                    details={
                        "path": f"{path}.value[{vi}]",
                        "why_invalid": "IN-list elements must be literal scalars.",
                    },
                )
    return Filter(field=field, op=str(item.get("op", "=")), value=value)


_CHILD_GROUP_KEYS = ("child", "match", "where")


def _where_item_from_payload(item: Any, index: int) -> WhereItem:
    if is_child_group(item):
        return _child_group_from_payload(item, f"where[{index}]")
    return _filter_from_payload(item, f"where[{index}]")


def _child_group_from_payload(item: dict[str, Any], path: str) -> ChildGroup:
    """``{child, match, where}``: one child row meets every condition (``any``) or none does."""

    def invalid(message: str, at: str, why: str, **details: Any) -> SemanticLayerError:
        return SemanticLayerError(
            "INVALID_QUERY",
            message,
            details={"path": at, "why_invalid": why, **details},
        )

    unknown = sorted(set(item) - set(_CHILD_GROUP_KEYS))
    if unknown:
        raise invalid(
            f"{path} has keys a child group does not take: {unknown}",
            path,
            "A child group is {child, match, where}; a plain filter is {field, op, value}.",
            unsupported_keys=unknown,
            supported_keys=list(_CHILD_GROUP_KEYS),
        )
    child = item.get("child")
    if not isinstance(child, str) or not child.strip():
        raise invalid(
            f"{path}.child must be an entity id",
            f"{path}.child",
            "A child group names the entity whose rows its conditions apply to.",
        )
    match = item.get("match")
    if match not in CHILD_GROUP_MATCHES:
        raise invalid(
            f"{path}.match must be one of {list(CHILD_GROUP_MATCHES)}",
            f"{path}.match",
            "'any' keeps a row with at least one child row meeting every condition; "
            "'none' keeps a row with no such child row.",
            supported_values=list(CHILD_GROUP_MATCHES),
        )
    conditions = item.get("where")
    if not isinstance(conditions, list) or not conditions:
        raise invalid(
            f"{path}.where must be a non-empty list of filters",
            f"{path}.where",
            "A child group's conditions are filters {field, op, value} on the child.",
        )
    filters: list[Filter] = []
    for position, condition in enumerate(conditions):
        at = f"{path}.where[{position}]"
        if is_child_group(condition):
            raise invalid(
                f"{at} is a child group inside a child group",
                at,
                "Child groups do not nest; each one is a where item of its own.",
            )
        filters.append(_filter_from_payload(condition, at))
    return ChildGroup(child=child.strip(), match=match, where=filters)


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


QUERY_INPUT_KEYS: frozenset[str] = frozenset(
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
        "route_decisions",
        "observation_scope",
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
    unknown = sorted(key for key in set(payload) - QUERY_INPUT_KEYS if not key.startswith("_"))
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
            f"Valid top-level keys: {sorted(QUERY_INPUT_KEYS)}."
        )
        hint["suggested_query_ir_change"] = {"remove": [bad_key]}
    raise SemanticLayerError(
        "INVALID_QUERY",
        f"Query payload contains unsupported top-level key(s): {unknown}",
        details={
            "path": bad_key,
            "unsupported_keys": unknown,
            "supported_keys": sorted(QUERY_INPUT_KEYS),
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


_BARE_SELECT_MEASURE_KEYS: frozenset[str] = frozenset({"measure", "aggregation", "as"})
_SELECT_TARGET_KEYS: frozenset[str] = frozenset({"metric", "measure", "dimension"})


def _bare_select_expression(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return the canonical expression for a select item sent without ``expression``.

    Only the two unambiguous shapes convert: ``{metric, as?}`` and
    ``{measure, aggregation?, as?}``. Anything else (both ``metric`` and ``measure``,
    ``aggregation`` on a metric, unknown keys) returns None and stays an error.
    """
    keys = set(row) - {"as"}
    if keys == {"metric"}:
        return {"kind": "metric", "metric": row["metric"]}
    if "measure" in keys and keys <= _BARE_SELECT_MEASURE_KEYS:
        return {"kind": "measure", **{key: row[key] for key in keys}}
    return None


def _reject_select_row(
    row: dict[str, Any], idx: int, why: str, *, hint_code: str = "WRAP_SELECT_EXPRESSION"
) -> SemanticLayerError:
    return SemanticLayerError(
        "INVALID_EXPRESSION_AST",
        (
            f"select[{idx}] {why}. Send one select item per metric or measure, "
            '{"expression": {"kind": "measure", "measure": "<measure id>", "aggregation": '
            '"sum"}, "as": "<alias>"} or '
            '{"expression": {"kind": "metric", "metric": "<metric id>"}, "as": "<alias>"}, '
            'and list each dimension in group_by: ["<dimension id>"].'
        ),
        details={
            "path": f"select[{idx}]",
            "received_keys": sorted(row),
            "recovery_hints": [
                {
                    "code": hint_code,
                    "message": (
                        "Each select item is {expression, as} for one metric (kind 'metric') or "
                        "measure (kind 'measure', with 'aggregation'). Dimensions go in "
                        "group_by, not in select, and cannot carry an alias there."
                    ),
                }
            ],
        },
    )


def _rewrite_select_item(
    row: dict[str, Any], idx: int, group_by: list[str], *, partial: bool
) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
    """Decide one select item: ``(item to keep or None, dimension moved to group_by, note)``.

    The one place a shorthand is accepted or refused. An item that would lose a key
    (a dimension beside a metric, measure, alias or unknown key) is refused, never
    rewritten.
    """
    path = f"select[{idx}]"
    if "expression" in row:
        named = sorted(_SELECT_TARGET_KEYS & set(row))
        if named:
            raise _reject_select_row(row, idx, f"has 'expression' and also {named}")
        expression = row["expression"]
        if not isinstance(expression, dict) or set(expression) != {"dimension"}:
            return row, "", None
        dim_id = str(expression["dimension"] or "").strip()
        # With other group_by entries, adding this one would be a guess: the
        # expression parser keeps its MOVE_DIMENSION_TO_GROUP_BY error for that.
        if not dim_id or (group_by and dim_id not in group_by):
            return row, "", None
        extra = sorted(set(row) - {"expression"})
        if extra:
            raise _reject_select_row(
                row,
                idx,
                f"moves a dimension to group_by, which has no place for {extra}",
                hint_code="MOVE_DIMENSION_TO_GROUP_BY",
            )
        return None, dim_id, _note(path, row, {"group_by": [dim_id]})
    if "dimension" in row:
        extra = sorted(set(row) - {"dimension"})
        if extra:
            raise _reject_select_row(
                row,
                idx,
                f"names a dimension together with {extra}, and group_by has no place for them",
                hint_code="MOVE_DIMENSION_TO_GROUP_BY",
            )
        dim_id = str(row["dimension"] or "").strip()
        if not dim_id:
            raise SemanticLayerError(
                "INVALID_QUERY", f"select[{idx}].dimension must be a non-empty dimension id"
            )
        return None, dim_id, _note(path, row, {"group_by": [dim_id]})
    if partial and set(row) <= {"as"}:
        return row, "", None
    expression = _bare_select_expression(row)
    if expression is None:
        raise _reject_select_row(
            row, idx, f"has no 'expression', and its keys {sorted(row)} do not match one shape"
        )
    canonical = {"expression": expression, **({"as": row["as"]} if "as" in row else {})}
    return canonical, "", _note(path, row, canonical)


def _note(path: str, received: dict[str, Any], canonical: dict[str, Any]) -> dict[str, Any]:
    return {"path": path, "received": dict(received), "canonical": canonical}


def rewrite_select_shorthand(
    payload: dict[str, Any], partial: bool = False
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return ``payload`` with select shorthands rewritten, and a note per rewrite.

    Every query entry point (validate, compile, execute, plan) rewrites through this one
    function. validate, compile and execute report its notes as response warnings; plan
    discards them and returns the canonical form in ``best.query_ir``. The returned payload
    is the canonical form: rewriting it again changes nothing.
    """
    raw_select = payload.get("select", [])
    if not isinstance(raw_select, list):
        return payload, []
    group_by = _normalize_group_by_list(payload.get("group_by", []))
    select: list[Any] = []
    moved: list[str] = []
    notes: list[dict[str, Any]] = []
    for idx, row in enumerate(raw_select):
        if not isinstance(row, dict):
            raise SemanticLayerError("INVALID_QUERY", f"select[{idx}] must be an object")
        kept, dim_id, note = _rewrite_select_item(row, idx, group_by, partial=partial)
        if note is None:
            select.append(row)
            continue
        if kept is not None:
            select.append(kept)
        if dim_id and dim_id not in group_by and dim_id not in moved:
            moved.append(dim_id)
        notes.append(note)
    if not notes:
        return payload, []
    return {**payload, "select": select, "group_by": [*group_by, *moved]}, notes


def normalize_query(
    payload: dict[str, Any], *, config: PackageConfig | None = None
) -> NormalizedQuery:
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
    payload, _ = rewrite_select_shorthand(payload)
    select: list[QuerySelect] = []
    group_by = _normalize_group_by_list(payload.get("group_by", []))
    for idx, row in enumerate(list(payload.get("select", []) or [])):
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
        _where_item_from_payload(item, idx)
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
    time = _time_spec_from_payload(
        payload.get("time"),
        policy_context=dict(payload.get("policy_context", {}) or {}),
        config=config,
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
        order_by=order_by,
        limit=limit_value,
        debug=bool(payload.get("debug", False)),
        explain=bool(payload.get("explain", False)),
        export=bool(payload.get("export", False)),
        route_decisions=route_decisions_from_payload(payload),
        observation_scope=_observation_scope(payload),
    )


def _observation_scope(payload: dict[str, Any]) -> str:
    raw = payload.get("observation_scope")
    if raw is None or raw in OBSERVATION_SCOPES:
        return raw or ""
    raise SemanticLayerError(
        "INVALID_QUERY",
        f"observation_scope must be one of {list(OBSERVATION_SCOPES)}, got {raw!r}",
        details={"path": "observation_scope", "allowed": list(OBSERVATION_SCOPES)},
    )


def normalize_partial_query(
    payload: dict[str, Any], *, config: PackageConfig | None = None
) -> PartialQueryState:
    # Same silent-drift defense as normalize_query — build_options /
    # plan also accept partial query payloads and should reject
    # unknown top-level keys with the same USE_CANONICAL_KEY hint.
    _check_unknown_top_level_keys(payload)
    _check_supported_version(payload)
    route_decisions_from_payload(payload)
    payload, _ = rewrite_select_shorthand(payload, partial=True)
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
        _where_item_from_payload(item, idx)
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
        config=config,
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
    )
