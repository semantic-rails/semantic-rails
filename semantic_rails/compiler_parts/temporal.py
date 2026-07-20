from __future__ import annotations

import difflib
from collections.abc import Iterable
from typing import Any

from ..ast import NormalizedQuery
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
    expr_kind,
    expr_to_dict,
)
from ..schema import MetricConfig, PackageConfig
from .indexes import (
    _dimension_index,
    _measure_index,
    _recipe_index,
    _temporal_role_index,
    get_package_analysis,
)


def _combine_temporal_roles(role_sets: Iterable[set[str]]) -> set[str]:
    filtered = [set(item) for item in role_sets if item]
    if not filtered:
        return set()
    current = set(filtered[0])
    for item in filtered[1:]:
        current &= item
    return current


def _recipe_compatible_temporal_roles(
    recipe: MetricConfig, config: PackageConfig, query: NormalizedQuery
) -> set[str]:
    if recipe.compatible_temporal_roles:
        return set(recipe.compatible_temporal_roles)
    if recipe.temporal_role:
        return {recipe.temporal_role}
    return _expr_compatible_temporal_roles(recipe.expression, config, query)


def _time_bound_relationship_ids(query: NormalizedQuery, config: PackageConfig) -> set[str]:
    if query.time is None:
        return set()
    return set(get_package_analysis(config).temporal_relationship_ids)


def _requires_query_time(expr: SemanticExpr, config: PackageConfig) -> bool:
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        if recipe is None:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND", f"Unknown metric recipe '{expr.metric_recipe}'"
            )
        return _requires_query_time(recipe.expression, config)
    if isinstance(
        expr, (CumulativeExpr, RollingExpr, PriorPeriodExpr, PeriodToDateExpr, OffsetWindowExpr)
    ):
        return True
    if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
        return _requires_query_time(expr.left, config) or _requires_query_time(expr.right, config)
    if isinstance(expr, RatioExpr):
        return _requires_query_time(expr.numerator, config) or _requires_query_time(
            expr.denominator, config
        )
    if isinstance(expr, EntityValueExpr):
        return _requires_query_time(expr.input, config)
    if isinstance(expr, DistributionExpr):
        return _requires_query_time(expr.over, config)
    if isinstance(expr, BooleanExpr):
        return any(_requires_query_time(arg, config) for arg in expr.args)
    if isinstance(expr, CallExpr):
        return any(_requires_query_time(arg, config) for arg in expr.args)
    if isinstance(expr, CaseExpr):
        return any(
            _requires_query_time(item.when, config) or _requires_query_time(item.then, config)
            for item in expr.whens
        ) or (expr.else_expr is not None and _requires_query_time(expr.else_expr, config))
    if isinstance(expr, MetricPredicateExpr):
        return False
    if isinstance(expr, ConversionExpr):
        return False
    return False


def _window_unit_to_rows(unit: str, value: int, grain: str) -> int:
    unit_norm = str(unit).lower()
    grain_norm = str(grain).lower()
    fixed_steps = {
        ("day", "day"): 1,
        ("day", "week"): 7,
        ("week", "week"): 1,
        ("month", "month"): 1,
        ("month", "quarter"): 3,
        ("month", "year"): 12,
        ("quarter", "quarter"): 1,
        ("quarter", "year"): 4,
        ("year", "year"): 1,
    }
    step = fixed_steps.get((grain_norm, unit_norm))
    if step is None:
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED",
            f"Window unit '{unit}' is not supported for query grain '{grain}'",
            details={"unit": unit, "grain": grain},
        )
    return step * max(int(value), 0)


def _period_to_date_period(period: str, grain: str) -> str:
    period_norm = str(period).lower()
    supported = {"week", "month", "quarter", "year"}
    if period_norm not in supported:
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED",
            f"Period-to-date period '{period}' is not supported",
            details={"period": period},
        )
    order = {"day": 1, "week": 2, "month": 3, "quarter": 4, "year": 5}
    grain_norm = str(grain).lower()
    if order.get(period_norm, 0) < order.get(grain_norm, 0):
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED",
            f"Period-to-date period '{period}' is finer than query grain '{grain}'",
            details={"period": period, "grain": grain},
        )
    return period_norm


def _expr_compatible_temporal_roles(
    expr: SemanticExpr, config: PackageConfig, query: NormalizedQuery
) -> set[str]:
    measures = _measure_index(config)
    recipes = _recipe_index(config)

    if isinstance(expr, (MeasureRefExpr, AggregateExpr, ScopedAggregateExpr)):
        measure = measures.get(expr.measure)
        if measure is None:
            raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown measure '{expr.measure}'")
        if expr.temporal_role:
            return {expr.temporal_role}
        if expr.measure in query.temporal_role_overrides:
            return {query.temporal_role_overrides[expr.measure]}
        return set(measure.compatible_temporal_roles)
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = recipes.get(expr.metric_recipe)
        if recipe is None:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND", f"Unknown metric recipe '{expr.metric_recipe}'"
            )
        return _recipe_compatible_temporal_roles(recipe, config, query)
    if isinstance(expr, LiteralExpr):
        return set()
    if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
        return _combine_temporal_roles(
            [
                _expr_compatible_temporal_roles(expr.left, config, query),
                _expr_compatible_temporal_roles(expr.right, config, query),
            ]
        )
    if isinstance(expr, RatioExpr):
        return _combine_temporal_roles(
            [
                _expr_compatible_temporal_roles(expr.numerator, config, query),
                _expr_compatible_temporal_roles(expr.denominator, config, query),
            ]
        )
    if isinstance(expr, EntityValueExpr):
        return _expr_compatible_temporal_roles(expr.input, config, query)
    if isinstance(expr, DistributionExpr):
        return _expr_compatible_temporal_roles(expr.over, config, query)
    if isinstance(expr, BooleanExpr):
        return _combine_temporal_roles(
            _expr_compatible_temporal_roles(arg, config, query) for arg in expr.args
        )
    if isinstance(expr, CallExpr):
        return _combine_temporal_roles(
            _expr_compatible_temporal_roles(arg, config, query) for arg in expr.args
        )
    if isinstance(expr, CaseExpr):
        role_sets: list[set[str]] = []
        for item in expr.whens:
            role_sets.append(_expr_compatible_temporal_roles(item.when, config, query))
            role_sets.append(_expr_compatible_temporal_roles(item.then, config, query))
        if expr.else_expr is not None:
            role_sets.append(_expr_compatible_temporal_roles(expr.else_expr, config, query))
        return _combine_temporal_roles(role_sets)
    if isinstance(
        expr, (CumulativeExpr, RollingExpr, PriorPeriodExpr, PeriodToDateExpr, OffsetWindowExpr)
    ):
        return _expr_compatible_temporal_roles(expr.input, config, query)
    if isinstance(expr, MetricPredicateExpr):
        return set()
    if isinstance(expr, ConversionExpr):
        return _expr_compatible_temporal_roles(expr.base, config, query)
    return set()


def _expr_leaf_temporal_role_sets(
    expr: SemanticExpr, config: PackageConfig, query: NormalizedQuery
) -> list[set[str]]:
    measures = _measure_index(config)
    recipes = _recipe_index(config)

    if isinstance(expr, (MeasureRefExpr, AggregateExpr, ScopedAggregateExpr)):
        measure = measures.get(expr.measure)
        if measure is None:
            raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown measure '{expr.measure}'")
        if expr.temporal_role:
            return [{expr.temporal_role}]
        if expr.measure in query.temporal_role_overrides:
            return [{query.temporal_role_overrides[expr.measure]}]
        return [set(measure.compatible_temporal_roles)]
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = recipes.get(expr.metric_recipe)
        if recipe is None:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND", f"Unknown metric recipe '{expr.metric_recipe}'"
            )
        return _expr_leaf_temporal_role_sets(recipe.expression, config, query)
    if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
        return [
            *_expr_leaf_temporal_role_sets(expr.left, config, query),
            *_expr_leaf_temporal_role_sets(expr.right, config, query),
        ]
    if isinstance(expr, RatioExpr):
        return [
            *_expr_leaf_temporal_role_sets(expr.numerator, config, query),
            *_expr_leaf_temporal_role_sets(expr.denominator, config, query),
        ]
    if isinstance(expr, EntityValueExpr):
        return _expr_leaf_temporal_role_sets(expr.input, config, query)
    if isinstance(expr, DistributionExpr):
        return _expr_leaf_temporal_role_sets(expr.over, config, query)
    if isinstance(expr, BooleanExpr):
        out: list[set[str]] = []
        for arg in expr.args:
            out.extend(_expr_leaf_temporal_role_sets(arg, config, query))
        return out
    if isinstance(expr, CallExpr):
        call_out: list[set[str]] = []
        for arg in expr.args:
            call_out.extend(_expr_leaf_temporal_role_sets(arg, config, query))
        return call_out
    if isinstance(expr, CaseExpr):
        case_out: list[set[str]] = []
        for item in expr.whens:
            case_out.extend(_expr_leaf_temporal_role_sets(item.when, config, query))
            case_out.extend(_expr_leaf_temporal_role_sets(item.then, config, query))
        if expr.else_expr is not None:
            case_out.extend(_expr_leaf_temporal_role_sets(expr.else_expr, config, query))
        return case_out
    if isinstance(
        expr, (CumulativeExpr, RollingExpr, PriorPeriodExpr, PeriodToDateExpr, OffsetWindowExpr)
    ):
        return _expr_leaf_temporal_role_sets(expr.input, config, query)
    if isinstance(expr, ConversionExpr):
        return [
            *_expr_leaf_temporal_role_sets(expr.base, config, query),
            *_expr_leaf_temporal_role_sets(expr.converted, config, query),
        ]
    return []


def _query_allows_metric_time_alignment(
    query: NormalizedQuery, config: PackageConfig, expressions: list[SemanticExpr]
) -> bool:
    if query.time is None or not query.time.grain or not query.time.temporal_role:
        return False
    leaf_role_sets: list[set[str]] = []
    for expr in expressions:
        leaf_role_sets.extend(_expr_leaf_temporal_role_sets(expr, config, query))
    if len(leaf_role_sets) < 2:
        return False
    concrete_roles = {role for role_set in leaf_role_sets for role in role_set}
    return query.time.temporal_role in concrete_roles and any(
        query.time.temporal_role not in role_set for role_set in leaf_role_sets
    )


# TODO(point-in-time-joins): the runtime does not yet support pinning a
# slowly-changing dimension to its value as of an arbitrary anchor time in
# the query (for example, "store name as it was at month-start"). The
# closest behavior today is the coarse_snapshot_alignment heuristic below,
# which lets a snapshot role line up with calendar/as_of roles at month or
# coarser. A general as-of dimension join is documented as a gap in
# docs/CAPABILITIES.md under "What This Layer Does NOT Do Yet". When this
# lands, lift the gap entry and the marker together.
def _allows_coarse_snapshot_alignment(
    requested: str, compatible: set[str], query: NormalizedQuery, config: PackageConfig
) -> bool:
    if query.time is None:
        return False
    grain = str(query.time.grain or "").lower()
    if grain not in {"month", "quarter", "year"}:
        return False
    roles = _temporal_role_index(config)
    requested_role = roles.get(requested)
    compatible_roles = [roles.get(role_id) for role_id in compatible]
    if requested_role is None or any(role is None for role in compatible_roles):
        return False
    alignable_classes = {"as_of_time", "calendar_time"}
    if requested_role.temporal_class not in alignable_classes:
        return False
    if any(
        role.temporal_class not in alignable_classes
        for role in compatible_roles
        if role is not None
    ):
        return False
    if grain not in requested_role.supported_grains:
        return False
    return all(grain in role.supported_grains for role in compatible_roles if role is not None)


def _validate_calendar_id(query: NormalizedQuery, config: PackageConfig) -> None:
    """Reject queries that request a non-default `calendar_id` on a temporal
    role whose underlying entity isn't bound to that calendar — UNLESS the
    query also opts into `fill: true`, which routes through the calendar
    entity for grain-aligned bucketing.

    Without this check, the planner emits Gregorian SQL byte-identical to
    `calendar_id: "default"` while the user believes they're querying
    fiscal grains — a silently-wrong-answer failure mode.

    Two valid configurations:

    1. `time.temporal_role` is itself bound to the requested calendar
       (the role's underlying entity has `calendar_id: <name>`).
    2. `time.fill: true` is set; the planner joins the calendar entity
       and uses its month_start/quarter_start/etc. columns for buckets.
    """
    assert query.time is not None  # caller guarantees query.time is set
    requested_calendar = (query.time.calendar_id or "default").strip().lower()
    if requested_calendar in {"", "default"}:
        return
    # Path 2: fill: true routes through the calendar entity directly.
    if query.time.fill:
        return
    role = _temporal_role_index(config).get(query.time.temporal_role)
    if role is None:
        return  # Already raised above; defensive.
    dim = _dimension_index(config).get(role.dimension)
    if dim is None:
        return
    entity = next((row for row in config.entities if row.id == dim.entity), None)
    role_calendar = (entity.calendar_id if entity else "").strip().lower() or "default"
    # Path 1: the role is itself bound to the requested calendar.
    if role_calendar == requested_calendar:
        return
    # Build the list of compatible roles bound to the requested calendar
    # so the recovery hint can point users to a working alternative.
    compatible_roles = []
    for r in config.temporal_roles:
        rd = _dimension_index(config).get(r.dimension)
        if rd is None:
            continue
        re = next((row for row in config.entities if row.id == rd.entity), None)
        rc = (re.calendar_id if re else "").strip().lower() or "default"
        if rc == requested_calendar:
            compatible_roles.append(r.id)
    raise SemanticLayerError(
        "INCOMPATIBLE_CALENDAR",
        (
            f"Temporal role '{query.time.temporal_role}' is bound to calendar "
            f"'{role_calendar}' but the query requests calendar "
            f"'{requested_calendar}'. The calendar_id parameter does not switch "
            f"the role's calendar on its own — pick a temporal role that's "
            f"bound to the requested calendar, or set time.fill: true to route "
            f"through the calendar entity's grain columns."
        ),
        details={
            "requested_calendar": requested_calendar,
            "role_calendar": role_calendar,
            "temporal_role": query.time.temporal_role,
            "alternative_temporal_roles": sorted(compatible_roles),
        },
    )


def _collect_conversion_bindings(
    expr: SemanticExpr,
    config: PackageConfig,
    out: list[tuple[ConversionExpr, str]],
    *,
    recipe_id: str = "",
) -> None:
    """Walk an expression AST and collect every `ConversionExpr` along with
    the metric recipe id it was resolved through (if any).

    The recipe id matters because the recipe's declared
    ``temporal_role``/``compatible_temporal_roles`` is the user-facing
    contract a caller must satisfy. A bare conversion expression (no
    recipe) is bound only by its base anchor's compatible temporal roles.
    """
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        if recipe is None:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND", f"Unknown metric recipe '{expr.metric_recipe}'"
            )
        _collect_conversion_bindings(
            recipe.expression, config, out, recipe_id=str(expr.metric_recipe)
        )
        return
    if isinstance(expr, ConversionExpr):
        out.append((expr, recipe_id))
        # Note: we do NOT descend further. Conversion is a leaf semantic
        # form for the temporal-binding check.
        return
    if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
        _collect_conversion_bindings(expr.left, config, out, recipe_id=recipe_id)
        _collect_conversion_bindings(expr.right, config, out, recipe_id=recipe_id)
        return
    if isinstance(expr, RatioExpr):
        _collect_conversion_bindings(expr.numerator, config, out, recipe_id=recipe_id)
        _collect_conversion_bindings(expr.denominator, config, out, recipe_id=recipe_id)
        return
    if isinstance(expr, BooleanExpr):
        for arg in expr.args:
            _collect_conversion_bindings(arg, config, out, recipe_id=recipe_id)
        return
    if isinstance(expr, CallExpr):
        for arg in expr.args:
            _collect_conversion_bindings(arg, config, out, recipe_id=recipe_id)
        return
    if isinstance(expr, CaseExpr):
        for item in expr.whens:
            _collect_conversion_bindings(item.when, config, out, recipe_id=recipe_id)
            _collect_conversion_bindings(item.then, config, out, recipe_id=recipe_id)
        if expr.else_expr is not None:
            _collect_conversion_bindings(expr.else_expr, config, out, recipe_id=recipe_id)
        return
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
        _collect_conversion_bindings(expr.input, config, out, recipe_id=recipe_id)
        return
    if isinstance(expr, EntityValueExpr):
        _collect_conversion_bindings(expr.input, config, out, recipe_id=recipe_id)
        return
    if isinstance(expr, DistributionExpr):
        _collect_conversion_bindings(expr.over, config, out, recipe_id=recipe_id)
        return


def _conversion_allowed_temporal_roles(
    conversion: ConversionExpr,
    recipe_id: str,
    config: PackageConfig,
    query: NormalizedQuery,
) -> tuple[set[str], str]:
    """Compute the set of temporal roles a conversion's top-level
    ``query.time`` block can legally target.

    The conversion-leaf CTE pins a *single* anchor relation: the base
    side. Any time filter the planner attaches to the leaf CTE has to
    resolve to a column on that anchor relation (or be aligned to it).
    Returns ``(allowed_roles, anchor_role)`` where ``anchor_role`` is the
    base side's resolved time role — the surest-to-work suggestion.
    """
    # Prefer the recipe's published contract when the conversion came
    # from a curated metric.
    recipe = _recipe_index(config).get(recipe_id) if recipe_id else None
    if recipe is not None and (recipe.compatible_temporal_roles or recipe.temporal_role):
        allowed: set[str] = set(recipe.compatible_temporal_roles)
        if recipe.temporal_role:
            allowed.add(recipe.temporal_role)
        anchor = recipe.temporal_role or next(iter(allowed))
        return allowed, anchor
    # Bare/inline conversion: the contract is whatever the base measure
    # declares as its compatible temporal roles, with a fallback to
    # whatever the converted side declares iff the converted side shares
    # the base entity (i.e. the CTE relation is identical).
    base_roles = _expr_compatible_temporal_roles(conversion.base, config, query)
    converted_roles = _expr_compatible_temporal_roles(conversion.converted, config, query)
    allowed = set(base_roles)
    if base_roles and converted_roles and base_roles == converted_roles:
        allowed |= converted_roles
    anchor = next(iter(base_roles)) if base_roles else ""
    return allowed, anchor


def _validate_conversion_temporal_bindings(query: NormalizedQuery, config: PackageConfig) -> None:
    """Pre-validation gate for conversion metrics.

    Compile produces a conversion-leaf CTE that's pinned to the base
    side's anchor relation. The top-level ``time`` block attaches a
    filter to *that* CTE. If the requested ``temporal_role`` resolves to
    a column on a different relation, the rendered SQL references a
    table the CTE never selects — a binder error at execute time that
    ``validate``/``compile`` previously failed to catch.

    This gate fires before ``_validate_query_temporal_bindings`` so the
    error is the precise ``INVALID_TEMPORAL_BINDING`` envelope rather
    than the broader ``INCOMPATIBLE_TEMPORAL_ROLE`` (which would also
    fire, but with weaker hints).
    """
    if query.time is None or not query.time.temporal_role:
        return
    expressions = [item.expression for item in query.select if item.expression is not None]
    expressions.extend(
        item.expression for item in query.metric_filters if item.expression is not None
    )
    conversions: list[tuple[ConversionExpr, str]] = []
    for expr in expressions:
        _collect_conversion_bindings(expr, config, conversions)
    if not conversions:
        return
    requested = query.time.temporal_role
    for conversion, recipe_id in conversions:
        allowed, anchor = _conversion_allowed_temporal_roles(conversion, recipe_id, config, query)
        if not allowed:
            # The conversion's base side declared no temporal roles at
            # all. Let the downstream binder report it — there's no
            # actionable hint we can produce here.
            continue
        if requested in allowed:
            continue
        details: dict[str, Any] = {
            "requested": requested,
            "allowed_temporal_roles": sorted(allowed),
            "anchor_temporal_role": anchor,
            "expression": expr_to_dict(conversion),
        }
        if recipe_id:
            details["metric"] = recipe_id
        raise SemanticLayerError(
            "INVALID_TEMPORAL_BINDING",
            (
                f"Temporal role '{requested}' cannot bind to the conversion's "
                f"leaf relation. Conversion CTEs are anchored on the base side "
                f"and only the base anchor's clock (or a recipe-declared "
                f"compatible role) is in scope for the top-level time filter."
            ),
            details=details,
        )


def _validate_grain_against_temporal_role(query: NormalizedQuery, role: Any) -> None:
    """Reject ``time.grain`` values that aren't in the temporal role's
    ``supported_grains`` list.

    Without this check, a typo like ``grain: "fortnight"`` flows past
    compile and only fails at execute when the warehouse rejects the
    DATE_TRUNC specifier — the agent gets a raw driver error with no
    structured recovery path.

    Casing decision: grain is normalized to lowercase before comparison,
    so ``"Month"`` is accepted as ``"month"``. The codebase consistently
    calls ``.lower()`` on grain at every downstream use site (compiler.py,
    relation_pipelines.py, temporal.py itself), so accepting mixed case
    here matches the de-facto invariant. Empty grain (default) is allowed
    — separate warnings handle ungrained-time-projection cases.
    """
    assert query.time is not None  # caller guarantees query.time is set
    raw_grain = str(query.time.grain or "").strip()
    if not raw_grain:
        return
    grain_norm = raw_grain.lower()
    supported = list(getattr(role, "supported_grains", []) or [])
    supported_sorted = sorted(supported)
    if grain_norm in supported:
        return
    closest = difflib.get_close_matches(grain_norm, supported_sorted, n=3, cutoff=0.4)
    raise SemanticLayerError(
        "INVALID_QUERY",
        (
            f"time.grain '{raw_grain}' is not supported by temporal role "
            f"'{role.id}'. Supported: {supported_sorted}."
        ),
        details={
            "path": "time.grain",
            "received": raw_grain,
            "supported_grains": supported_sorted,
            "temporal_role": role.id,
            "closest_matches": closest,
        },
    )


def _validate_query_temporal_bindings(query: NormalizedQuery, config: PackageConfig) -> None:
    expressions = [item.expression for item in query.select if item.expression is not None]
    expressions.extend(
        item.expression for item in query.metric_filters if item.expression is not None
    )
    if query.time is None:
        for expr in expressions:
            if _requires_query_time(expr, config):
                raise SemanticLayerError(
                    "INVALID_QUERY", f"Expression '{expr_kind(expr)}' requires query.time"
                )
        return
    requested = query.time.temporal_role
    role_index = _temporal_role_index(config)
    if requested not in role_index:
        raise SemanticLayerError("INVALID_TEMPORAL_ROLE", f"Unknown temporal role '{requested}'")
    _validate_grain_against_temporal_role(query, role_index[requested])
    _validate_calendar_id(query, config)
    # Conversion metrics get their temporal pairing validated up-front so
    # the failure mode is a precise INVALID_TEMPORAL_BINDING envelope,
    # not a downstream binder error at execute time.
    _validate_conversion_temporal_bindings(query, config)
    allow_metric_time_alignment = _query_allows_metric_time_alignment(query, config, expressions)
    for expr in expressions:
        compatible = _expr_compatible_temporal_roles(expr, config, query)
        if compatible and requested not in compatible:
            if allow_metric_time_alignment:
                continue
            if _allows_coarse_snapshot_alignment(requested, compatible, query, config):
                continue
            raise SemanticLayerError(
                "INCOMPATIBLE_TEMPORAL_ROLE",
                f"Temporal role '{requested}' is not compatible with expression '{expr_kind(expr)}'",
                details={
                    "requested": requested,
                    "compatible": sorted(compatible),
                    "expression": expr_to_dict(expr),
                },
            )


def _expr_contains_cumulative(expr: SemanticExpr, config: PackageConfig) -> bool:
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        if recipe is None:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND", f"Unknown metric recipe '{expr.metric_recipe}'"
            )
        return _expr_contains_cumulative(recipe.expression, config)
    if (
        isinstance(expr, (CumulativeExpr, OffsetWindowExpr))
        and getattr(expr, "kind", "cumulative") == "cumulative"
    ):
        return True
    if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
        return _expr_contains_cumulative(expr.left, config) or _expr_contains_cumulative(
            expr.right, config
        )
    if isinstance(expr, BooleanExpr):
        return any(_expr_contains_cumulative(arg, config) for arg in expr.args)
    if isinstance(expr, CallExpr):
        return any(_expr_contains_cumulative(arg, config) for arg in expr.args)
    if isinstance(expr, CaseExpr):
        return any(
            _expr_contains_cumulative(item.when, config)
            or _expr_contains_cumulative(item.then, config)
            for item in expr.whens
        ) or (expr.else_expr is not None and _expr_contains_cumulative(expr.else_expr, config))
    if isinstance(
        expr,
        (RollingExpr, PriorPeriodExpr, PeriodToDateExpr, OffsetWindowExpr, MetricPredicateExpr),
    ):
        return _expr_contains_cumulative(expr.input, config)
    if isinstance(expr, RatioExpr):
        return _expr_contains_cumulative(expr.numerator, config) or _expr_contains_cumulative(
            expr.denominator, config
        )
    if isinstance(expr, EntityValueExpr):
        return _expr_contains_cumulative(expr.input, config)
    if isinstance(expr, DistributionExpr):
        return _expr_contains_cumulative(expr.over, config)
    if isinstance(expr, ConversionExpr):
        return _expr_contains_cumulative(expr.base, config) or _expr_contains_cumulative(
            expr.converted, config
        )
    return False


def _expr_contains_unsafe_bounded_cumulative(expr: SemanticExpr, config: PackageConfig) -> bool:
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        if recipe is None:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND", f"Unknown metric recipe '{expr.metric_recipe}'"
            )
        return _expr_contains_unsafe_bounded_cumulative(recipe.expression, config)
    if isinstance(expr, CumulativeExpr):
        return True
    if isinstance(expr, OffsetWindowExpr) and expr.kind == "cumulative":
        return expr.window_scope != "query_period"
    if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
        return _expr_contains_unsafe_bounded_cumulative(
            expr.left, config
        ) or _expr_contains_unsafe_bounded_cumulative(expr.right, config)
    if isinstance(expr, BooleanExpr):
        return any(_expr_contains_unsafe_bounded_cumulative(arg, config) for arg in expr.args)
    if isinstance(expr, CallExpr):
        return any(_expr_contains_unsafe_bounded_cumulative(arg, config) for arg in expr.args)
    if isinstance(expr, CaseExpr):
        return any(
            _expr_contains_unsafe_bounded_cumulative(item.when, config)
            or _expr_contains_unsafe_bounded_cumulative(item.then, config)
            for item in expr.whens
        ) or (
            expr.else_expr is not None
            and _expr_contains_unsafe_bounded_cumulative(expr.else_expr, config)
        )
    if isinstance(
        expr,
        (RollingExpr, PriorPeriodExpr, PeriodToDateExpr, OffsetWindowExpr, MetricPredicateExpr),
    ):
        return _expr_contains_unsafe_bounded_cumulative(expr.input, config)
    if isinstance(expr, RatioExpr):
        return _expr_contains_unsafe_bounded_cumulative(
            expr.numerator, config
        ) or _expr_contains_unsafe_bounded_cumulative(expr.denominator, config)
    if isinstance(expr, EntityValueExpr):
        return _expr_contains_unsafe_bounded_cumulative(expr.input, config)
    if isinstance(expr, DistributionExpr):
        return _expr_contains_unsafe_bounded_cumulative(expr.over, config)
    if isinstance(expr, ConversionExpr):
        return _expr_contains_unsafe_bounded_cumulative(
            expr.base, config
        ) or _expr_contains_unsafe_bounded_cumulative(expr.converted, config)
    return False


def _expr_contains_unsafe_bounded_windowed(expr: SemanticExpr, config: PackageConfig) -> bool:
    """Walk the expression AST to find rolling/prior_period/period_to_date
    nodes that would silently truncate when the leaf SQL applies a hard
    `WHERE t >= start` filter.

    This mirrors `_expr_contains_unsafe_bounded_cumulative` but covers the
    three newer windowed kinds. `OffsetWindowExpr` with kind=='cumulative'
    is already handled by the cumulative walker; here we cover its other
    kinds (rolling, prior_period, period_to_date).
    """
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        if recipe is None:
            raise SemanticLayerError(
                "OBJECT_NOT_FOUND", f"Unknown metric recipe '{expr.metric_recipe}'"
            )
        return _expr_contains_unsafe_bounded_windowed(recipe.expression, config)
    if isinstance(expr, (RollingExpr, PriorPeriodExpr, PeriodToDateExpr)):
        return True
    if isinstance(expr, OffsetWindowExpr) and expr.kind in {
        "rolling",
        "prior_period",
        "period_to_date",
    }:
        return True
    if isinstance(expr, (ArithmeticExpr, ComparisonExpr)):
        return _expr_contains_unsafe_bounded_windowed(
            expr.left, config
        ) or _expr_contains_unsafe_bounded_windowed(expr.right, config)
    if isinstance(expr, BooleanExpr):
        return any(_expr_contains_unsafe_bounded_windowed(arg, config) for arg in expr.args)
    if isinstance(expr, CallExpr):
        return any(_expr_contains_unsafe_bounded_windowed(arg, config) for arg in expr.args)
    if isinstance(expr, CaseExpr):
        return any(
            _expr_contains_unsafe_bounded_windowed(item.when, config)
            or _expr_contains_unsafe_bounded_windowed(item.then, config)
            for item in expr.whens
        ) or (
            expr.else_expr is not None
            and _expr_contains_unsafe_bounded_windowed(expr.else_expr, config)
        )
    if isinstance(expr, (CumulativeExpr, MetricPredicateExpr, OffsetWindowExpr)):
        return _expr_contains_unsafe_bounded_windowed(expr.input, config)
    if isinstance(expr, RatioExpr):
        return _expr_contains_unsafe_bounded_windowed(
            expr.numerator, config
        ) or _expr_contains_unsafe_bounded_windowed(expr.denominator, config)
    if isinstance(expr, EntityValueExpr):
        return _expr_contains_unsafe_bounded_windowed(expr.input, config)
    if isinstance(expr, DistributionExpr):
        return _expr_contains_unsafe_bounded_windowed(expr.over, config)
    if isinstance(expr, ConversionExpr):
        return _expr_contains_unsafe_bounded_windowed(
            expr.base, config
        ) or _expr_contains_unsafe_bounded_windowed(expr.converted, config)
    return False


def _unsafe_windowed_lookback(expr: SemanticExpr, config: PackageConfig) -> dict[str, Any] | None:
    """Return the lookback (unit + value + originating kind) of the first
    unsafe windowed node in ``expr``, so the
    ``WINDOWED_TIME_FILTER_UNSUPPORTED`` recovery hint can suggest a
    concrete widened ``query.time.start``. ``None`` when no offending
    node is found — callers should already have established that
    ``_expr_contains_unsafe_bounded_windowed`` returned True.
    """
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = _recipe_index(config).get(expr.metric_recipe)
        if recipe is None:
            return None
        return _unsafe_windowed_lookback(recipe.expression, config)
    if isinstance(expr, RollingExpr):
        return {
            "kind": "RollingExpr",
            "unit": str(expr.unit or ""),
            "value": int(expr.value or 0),
        }
    if isinstance(expr, PriorPeriodExpr):
        return {
            "kind": "PriorPeriodExpr",
            "unit": str(expr.unit or ""),
            "value": int(expr.value or 0),
        }
    if isinstance(expr, PeriodToDateExpr):
        # Period-to-date looks back to the start of the period — one
        # unit of the period grain is the safe minimum widening.
        return {
            "kind": "PeriodToDateExpr",
            "unit": str(expr.period or ""),
            "value": 1,
        }
    if isinstance(expr, OffsetWindowExpr) and expr.kind in {
        "rolling",
        "prior_period",
        "period_to_date",
    }:
        # OffsetWindowExpr carries unit/value directly; period_to_date
        # variant uses the period grain instead.
        if expr.kind == "period_to_date":
            return {
                "kind": "OffsetWindowExpr[period_to_date]",
                "unit": str(expr.period or ""),
                "value": 1,
            }
        return {
            "kind": f"OffsetWindowExpr[{expr.kind}]",
            "unit": str(expr.unit or ""),
            "value": int(expr.value or 0),
        }
    walkable = getattr(expr, "__dict__", {})
    for attr in ("input", "left", "right", "numerator", "denominator", "over", "base", "converted"):
        child = walkable.get(attr) if isinstance(walkable, dict) else None
        if child is None:
            continue
        if isinstance(child, SemanticExpr):
            found = _unsafe_windowed_lookback(child, config)
            if found is not None:
                return found
    for collection_attr in ("args", "whens"):
        children = walkable.get(collection_attr) if isinstance(walkable, dict) else None
        if not children:
            continue
        for child in children:
            if isinstance(child, SemanticExpr):
                found = _unsafe_windowed_lookback(child, config)
                if found is not None:
                    return found
    return None


def _validate_restrictive_time_semantics(query: NormalizedQuery, config: PackageConfig) -> None:
    if query.time is None or query.time.start is None:
        return
    expressions = [item.expression for item in query.select if item.expression is not None]
    expressions.extend(
        item.expression for item in query.metric_filters if item.expression is not None
    )
    for expr in expressions:
        if _expr_contains_unsafe_bounded_cumulative(expr, config):
            raise SemanticLayerError(
                "CUMULATIVE_TIME_FILTER_UNSUPPORTED",
                "Cumulative expressions do not support a bounded query.time.start; widen the time window or remove the start boundary",
                details={"start": query.time.start, "expression": expr_to_dict(expr)},
            )
        if _expr_contains_unsafe_bounded_windowed(expr, config):
            lookback = _unsafe_windowed_lookback(expr, config)
            details: dict[str, Any] = {
                "start": query.time.start,
                "expression": expr_to_dict(expr),
            }
            if lookback is not None:
                details["lookback"] = lookback
            raise SemanticLayerError(
                "WINDOWED_TIME_FILTER_UNSUPPORTED",
                (
                    "Rolling, prior_period, and period_to_date expressions do not support a bounded "
                    "query.time.start: the leaf-level WHERE truncates the lookback rows the window "
                    "function depends on, producing silently wrong values. Remove the start boundary "
                    "or widen it by the metric's window/offset/period lookback."
                ),
                details=details,
            )
