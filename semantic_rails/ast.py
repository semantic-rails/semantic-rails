"""Query IR — the normalized in-memory form of an inbound Query IR.

Exposes :class:`NormalizedQuery` and :func:`normalize_query` /
:func:`normalize_partial_query`. Every public entry point (validate,
compile, explain, query) normalizes its input here before the compiler
runs, so the rest of the runtime can rely on a single canonical shape
regardless of which surface (HTTP, MCP, CLI) the request came in on.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from . import operators
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
    _closest_key_matches,
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
    """The query's time block, resolved: the clock, the grain and ``[start, end)``.

    ``last_unit``/``last_value`` and ``this`` keep the caller's relative spelling, so the answer
    can say what window it used; the request's ``now`` it was resolved against travels with
    the request (``policy_context["now"]``), never in the query.
    """

    temporal_role: str
    grain: str = ""
    start: Any = None
    end: Any = None
    fill: bool = False
    last_unit: str = ""
    last_value: int = 0
    this: str = ""

    @property
    def named(self) -> bool:
        """A named window (``last``, ``this``, or ``start`` and ``end``) answers with its rows."""
        return bool(
            self.last_unit or self.this or (self.start is not None and self.end is not None)
        )


# The output column of the time axis. A query without a grain has no time column (one total
# per group), except the engine's own raw-time sub-queries, which key on the clock's id.
TIME_AXIS = "period"
PERIOD_PARTIAL = "period_partial"


def time_output_alias(time: TimeSpec | dict[str, Any] | None) -> str:
    """The time axis column of a query: ``period`` when it has a grain."""
    if time is None:
        return ""
    role = time.get("temporal_role", "") if isinstance(time, dict) else time.temporal_role
    grain = time.get("grain", "") if isinstance(time, dict) else time.grain
    if not role:
        return ""
    return TIME_AXIS if grain else str(role)


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
        where = f"routes[{index}]"
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
        }


# Query IR fields: query patches, query state and caller selections carry only
# these, never request metadata, response options or the tool's own arguments.
_QUERY_IR_KEYS = tuple(query_field.name for query_field in fields(NormalizedQuery))


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


_TIME_KEYS = frozenset(
    {"temporal_role", "grain", "start", "end", "fill", "last_unit", "last_value", "this"}
)


def _time_spec_from_payload(
    payload: Any, *, allow_missing_temporal_role: bool = False
) -> TimeSpec | None:
    """The engine's own time block (the front door resolves the caller's spelling first)."""
    if not payload:
        return None
    if not isinstance(payload, dict):
        raise SemanticLayerError("INVALID_QUERY", "query.time must be an object")
    unknown_keys = sorted(set(payload) - _TIME_KEYS)
    if unknown_keys:
        raise SemanticLayerError(
            "INVALID_QUERY",
            "query.time contains unsupported keys",
            details={
                "path": "time",
                "unsupported_keys": unknown_keys,
                "supported_keys": sorted(_TIME_KEYS),
            },
        )
    temporal_role = str(payload.get("temporal_role", "")).strip()
    if not allow_missing_temporal_role and not temporal_role:
        raise SemanticLayerError("INVALID_QUERY", "query.time.temporal_role is required")
    return TimeSpec(
        temporal_role=temporal_role,
        grain=str(payload.get("grain", "") or ""),
        start=payload.get("start"),
        end=payload.get("end"),
        fill=bool(payload.get("fill", False)),
        last_unit=str(payload.get("last_unit", "") or ""),
        last_value=int(payload.get("last_value", 0) or 0),
        this=str(payload.get("this", "") or ""),
    )


_SELECT_ITEM_KEYS = frozenset({"expression", "as"})
_WHERE_FILTER_KEYS = frozenset({"field", "op", "value"})
_CHILD_GROUP_KEYS = frozenset({"child", "match", "where"})
_METRIC_FILTER_KEYS = frozenset({"expression", "op", "value"})


def _check_query_item_keys(item: dict[str, Any], keys: frozenset[str], path: str) -> None:
    """Query IR v1 item keys are closed; normalization must never drop one."""
    unknown = sorted(set(item) - keys)
    if not unknown:
        return
    details: dict[str, Any] = {
        "path": path,
        "unsupported_keys": unknown,
        "supported_keys": sorted(keys),
        "closest_matches": _closest_key_matches(unknown[0], keys),
    }
    if path.startswith("metric_filters[") and "entity" in unknown:
        details["recovery_hints"] = [
            {
                "code": "USE_METRIC_PREDICATE",
                "message": (
                    "To filter per entity, use expression.kind 'metric_predicate' with "
                    "entity inside the expression; metric_filters items have no entity key."
                ),
            }
        ]
    raise SemanticLayerError(
        "INVALID_QUERY", f"{path} contains unsupported key(s): {unknown}", details=details
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
    _check_query_item_keys(item, _WHERE_FILTER_KEYS, path)
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
    # A dict value would stringify into a broken comparison; thresholds computed from
    # data belong in their own query or in an entity filter's ``having``.
    if isinstance(value, dict):
        raise SemanticLayerError(
            "INVALID_QUERY",
            f"{path}.value must be a scalar (or a list for is / not), not an object.",
            details={
                "path": f"{path}.value",
                "received_kind": str(value.get("kind", "")),
                "why_invalid": "where filters compare a dimension to literal values.",
            },
        )
    op, value = operators.normalize_condition(item.get("op", "is"), value, path=path, internal=True)
    return Filter(field=field, op=op, value=value)


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

    _check_query_item_keys(item, _CHILD_GROUP_KEYS, path)
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
    _check_query_item_keys(item, _METRIC_FILTER_KEYS, f"metric_filters[{index}]")
    return item


def _metric_filter_op(item: dict[str, Any], index: int) -> str:
    """The word for a result filter's op (``value`` keeps its shape for the lowering)."""
    raw = item.get("op", "is")
    value = item.get("value")
    if value is None or isinstance(value, bool):
        spelling = " ".join(str(raw).strip().lower().split())
        if spelling in {"=", "==", "is", "eq"}:
            return "is"
        if spelling in {"!=", "<>", "not", "neq"}:
            return "not"
    return operators.operator_word(raw, path=f"metric_filters[{index}].op")


def _assert_unique_output_aliases(
    select: list[QuerySelect], group_by: list[str], time: TimeSpec | None
) -> None:
    alias_sources: dict[str, list[str]] = {}
    for index, dim_id in enumerate(group_by):
        alias_sources.setdefault(dim_id, []).append(f"group_by[{index}]")
    time_alias = time_output_alias(time)
    if time_alias:
        alias_sources.setdefault(time_alias, []).append("time")
        if time_alias == TIME_AXIS:
            alias_sources.setdefault(PERIOD_PARTIAL, []).append("time")
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
        _check_query_item_keys(row, _SELECT_ITEM_KEYS, path)
        expression = row["expression"]
        if (
            not isinstance(expression, dict)
            or set(expression) - {"kind"} != {"dimension"}
            or expression.get("kind", "dimension") not in ("dimension", "group", "ref")
        ):
            return row, "", None
        dim_id = str(expression["dimension"] or "").strip()
        if not dim_id:
            return row, "", None
        # With other group_by entries, adding this one would be a guess.
        if group_by and dim_id not in group_by:
            raise _reject_select_row(
                row,
                idx,
                "names a dimension absent from the existing group_by",
                hint_code="MOVE_DIMENSION_TO_GROUP_BY",
            )
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
        extra = sorted(set(row) - {"dimension", "kind"})
        if "kind" in row and row["kind"] not in ("dimension", "group", "ref"):
            extra.append("kind")
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
    discards them and returns the canonical form in ``best.query``. The returned payload
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
        expression = parse_semantic_expression(
            row.get("expression", {}) or {}, context="query", path=f"query.select[{idx}].expression"
        )
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
                    item.get("expression", {}) or {},
                    context="query",
                    path=f"query.metric_filters[{mf_idx}].expression",
                ),
                op=_metric_filter_op(item, mf_idx),
                value=mf_value,
            )
        )
    for item in metric_filters:
        assert (
            item.expression is not None
        )  # parse_semantic_expression returns non-None for non-empty payload
        _validate_query_expr(item.expression)
    time = _time_spec_from_payload(payload.get("time"))
    if not select and not group_by and time is None:
        raise SemanticLayerError(
            "INVALID_QUERY", "Query requires select expressions, group_by, or time"
        )
    _assert_unique_output_aliases(select, group_by, time)
    order_by = _order_by_from_payload(payload, select, group_by, time)
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
        route_decisions=route_decisions_from_payload(payload),
        observation_scope=_observation_scope(payload),
    )


def _order_by_from_payload(
    payload: dict[str, Any], select: list[QuerySelect], group_by: list[str], time: TimeSpec | None
) -> list[OrderBy]:
    """Each order_by field resolves to a select alias, a group_by dimension or the time axis.

    Without this, a typo in ``order_by[].field`` passes validate and then errors at the
    warehouse with a raw "column not found" message. The time axis is named ``period``
    (``time`` is the engine's own spelling) and resolves to the axis column.
    """
    time_alias = time_output_alias(time)
    select_aliases = {item.as_ for item in select}
    order_by: list[OrderBy] = []
    for item in list(payload.get("order_by", []) or []):
        field = str(expression_field(item, "field", expression_position="order_by"))
        # ``expression_field`` rejects non-dict entries first, so this ``.get`` is safe.
        direction = str(item.get("direction", "ASC")).upper()
        if field in {TIME_AXIS, "time"}:
            if not time_alias:
                raise SemanticLayerError(
                    "INVALID_ORDER_BY",
                    f"order_by '{field}' requires a time grain",
                    details={"path": "order_by", "field": field},
                )
            field = time_alias
        elif not (field in select_aliases or field in group_by or field == time_alias):
            raise SemanticLayerError(
                "INVALID_ORDER_BY",
                f"order_by field {field!r} does not resolve to a select alias, group_by "
                "dimension, or the time axis",
                details={
                    "field": field,
                    "available_select_aliases": sorted(select_aliases),
                    "available_group_by": sorted(group_by),
                    "available_time_axis": time_alias,
                },
            )
        order_by.append(OrderBy(field=field, direction=direction))
    return order_by


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
    route_decisions_from_payload(payload)
    payload, _ = rewrite_select_shorthand(payload, partial=True)
    select: list[QuerySelect] = []
    for idx, row in enumerate(list(payload.get("select", []) or [])):
        if not isinstance(row, dict):
            raise SemanticLayerError("INVALID_QUERY", f"select[{idx}] must be an object")
        raw_expr = row.get("expression", {}) or {}
        expression = (
            parse_semantic_expression(
                raw_expr, context="query", path=f"query.select[{idx}].expression"
            )
            if raw_expr
            else None
        )
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
                    item.get("expression", {}) or {},
                    context="query",
                    path=f"query.metric_filters[{mf_idx}].expression",
                )
                if item.get("expression")
                else None,
                op=_metric_filter_op(item, mf_idx),
                value=item.get("value"),
            )
        )
    for item in metric_filters:
        if item.expression is not None:
            _validate_query_expr(item.expression)
    group_by = _normalize_group_by_list(payload.get("group_by", []))
    time = _time_spec_from_payload(payload.get("time"), allow_missing_temporal_role=True)
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
