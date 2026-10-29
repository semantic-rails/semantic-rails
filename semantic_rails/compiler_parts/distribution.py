"""Distributions and combined answers: per-entity values compiled as their own query, and the
branches of an answer whose measures can't share one leaf.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import replace
from typing import Any

from ..ast import (
    NormalizedQuery,
    normalize_query,
)
from ..dialects import dialect_for_warehouse
from ..errors import SemanticLayerError
from ..expressions import (
    DistributionExpr,
    MetricPredicateExpr,
    MetricRecipeRefExpr,
    SemanticExpr,
    expr_to_dict,
)
from ..ir import (
    BoundMeasure,
    LogicalPlan,
)
from ..schema import (
    PackageConfig,
)
from ..sql_ast import (
    SqlCall,
    SqlCast,
    SqlCte,
    SqlField,
    SqlIdentifier,
    SqlJoin,
    SqlLiteral,
    SqlSelect,
    SqlTableRef,
)
from . import nested
from .bind import (
    _aggregation_expr,
    _bound_metric_predicates,
    _collect_measure_refs,
    _parse_public_expr,
)
from .dependencies import (
    binding_cut,
    owning,
    project_is_cut,
    recipe_objects,
)
from .empty_groups import (
    GUARDED_BASE,
    guard_empty_groups,
    refuse_unsettled,
    zero_outputs,
)
from .indexes import (
    _recipe_index,
)
from .lowering_common import (
    _and_conditions,
    _dialect,
    _expr_contains_distribution,
    _metric_filters_require_dense_series,
    _query_key_aliases,
    _query_metric_predicates,
    _value_filter_condition,
)
from .namespacing import _namespace_sql_select
from .paths import (
    _entity_key_dimension_ids,
    inner_lookups,
)
from .post_aggregation import (
    _expr_requires_dense_series,
)
from .predicate_sets import _requested_order
from .snapshot_reads import record_output_reads
from .time_series import _calendar_fill_binding, _series_base_ctes, _window_is_named


def _branch_context_query(
    plan: LogicalPlan,
    expression: dict[str, Any],
    alias: str,
    *,
    extra_group_by: list[str] | None = None,
) -> dict[str, Any]:
    query: dict[str, Any] = {
        "version": int(plan.query.get("version", 1) or 1),
        "select": [{"expression": expression, "as": alias}],
        "group_by": [*list(plan.group_by), *list(extra_group_by or [])],
    }
    if plan.time:
        query["time"] = dict(plan.time)
    if plan.query.get("where"):
        query["where"] = list(plan.query.get("where") or [])
    if plan.query.get("metric_filters"):
        query["metric_filters"] = _distribution_metric_filters(plan)
    if plan.query.get("temporal_role_overrides"):
        query["temporal_role_overrides"] = dict(plan.query.get("temporal_role_overrides") or {})
    return query


def _distribution_metric_filters(plan: LogicalPlan) -> list[dict[str, Any]]:
    """Only entity predicates can enter a distribution's finer-grained branches."""
    filters = list(plan.query.get("metric_filters") or [])
    if any(
        not isinstance(_parse_public_expr(dict(item["expression"])), MetricPredicateExpr)
        for item in filters
    ):
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED",
            "A having filter beside a distribution cannot be evaluated at the returned "
            "group's grain. Run the group-level filter without the distribution first.",
        )
    return filters


def _distribution_input_predicates(
    expr: SemanticExpr, config: PackageConfig, query: NormalizedQuery
) -> Iterator[MetricPredicateExpr]:
    """Inspect the same recipe-expanded leaves that binding will put in the branch."""
    bound_measures: list[BoundMeasure] = []
    # Only inspect grain here; predicate sources bind their own clocks when lowered.
    _collect_measure_refs(
        expr, config, replace(query, time=None, temporal_role_overrides={}), bound_measures
    )
    for bound in bound_measures:
        for predicate in _bound_metric_predicates(bound):
            yield predicate
            yield from _distribution_input_predicates(predicate.input, config, query)


def _distribution_select(
    expr: DistributionExpr, *, alias: str, plan: LogicalPlan, config: PackageConfig
) -> SqlSelect:
    # A dense per-entity series holds every entity in every period, so an entity with no rows
    # in a period (not yet created, or long gone) would enter that period's distribution.
    if _expr_requires_dense_series(expr.over.input, config) or _metric_filters_require_dense_series(
        plan, config
    ):
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED",
            "A distribution is not supported when its input or a metric filter has a rolling or "
            "prior-period window: it would count entities in periods where they have no rows.",
        )
    _distribution_metric_filters(plan)
    for predicate in [
        *_query_metric_predicates(plan),
        *_distribution_input_predicates(expr.over.input, config, normalize_query(plan.query)),
    ]:
        if predicate.scope_mode == "contextual" and predicate.entity != expr.over.entity:
            raise SemanticLayerError(
                "PREDICATE_CONTEXT_ENTITY_INCOMPATIBLE",
                "A distribution's per-entity grouping cannot inherit this contextual predicate. "
                "Use over: all_time or window (scope_mode: entity_only in a package metric), "
                "or a where filter.",
            )
    # The per-entity grain belongs to this expression, not the outer query.
    with binding_cut() if project_is_cut() or bool(expr.over.where) else nullcontext():
        entity_key_dims = _entity_key_dimension_ids(expr.over.entity, config)
    value_alias = "__entity_value"
    entity_value_query = _branch_context_query(
        plan,
        expr_to_dict(expr.over.input),
        value_alias,
        extra_group_by=entity_key_dims,
    )
    # Like a metric predicate's set, the per-entity values join their lookups INNER: a row whose
    # lookup of the entity finds no match is no entity, never one NULL entity of its own.
    with inner_lookups():
        sql_ast = nested.query_sql(
            config,
            entity_value_query,
            project_cut=project_is_cut() or bool(expr.over.where),
            guard_empty=False,
        )
    source_name = f"{alias}__entity_values"
    key_aliases = _query_key_aliases(plan)
    value_ref = SqlIdentifier(parts=[source_name, value_alias])
    function = str(expr.function or "").strip().lower()
    parameters = dict(expr.parameters or {})
    if function == "percentile":
        if expr.p is not None:
            parameters.setdefault("p", expr.p)
        aggregate_expr = _aggregation_expr(
            value_ref,
            "percentile",
            parameters=parameters,
            dialect=dialect_for_warehouse(config.package.warehouse),
        )
    elif function in {"median", "avg", "sum", "min", "max"}:
        aggregate_expr = _aggregation_expr(
            value_ref,
            function,
            parameters=parameters,
            dialect=dialect_for_warehouse(config.package.warehouse),
        )
    else:
        raise SemanticLayerError(
            "UNSUPPORTED_AGGREGATION", f"Unsupported distribution function '{expr.function}'"
        )
    return SqlSelect(
        ctes=[
            SqlCte(
                name=source_name,
                query=_namespace_sql_select(sql_ast, f"{source_name}__"),
            )
        ],
        select=[
            *[SqlField(SqlIdentifier(parts=[source_name, key]), key) for key in key_aliases],
            SqlField(aggregate_expr, alias),
        ],
        from_table=SqlTableRef(name=source_name),
        where=[
            _value_filter_condition(value_ref, item, config)
            for item in list(expr.over.where or [])
            if str(item.get("kind", "value_filter")) == "value_filter"
        ],
        group_by=[SqlIdentifier(parts=[source_name, key]) for key in key_aliases],
    )


def _single_expression_branch_select(
    expr: SemanticExpr, *, alias: str, plan: LogicalPlan, config: PackageConfig
) -> SqlSelect:
    if isinstance(expr, MetricRecipeRefExpr) and _expr_contains_distribution(expr, config):
        with recipe_objects(expr.metric_recipe):
            recipe = _recipe_index(config)[expr.metric_recipe]
            return _single_expression_branch_select(
                recipe.expression, alias=alias, plan=plan, config=config
            )
    if isinstance(expr, DistributionExpr):
        return _distribution_select(expr, alias=alias, plan=plan, config=config)
    if _expr_contains_distribution(expr, config):
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED",
            "A distribution nested in another expression cannot retain its grouping grain; "
            "select the distribution separately.",
        )
    return nested.query_sql(
        config, _branch_context_query(plan, expr_to_dict(expr), alias), project_cut=project_is_cut()
    )


def _lower_agent_dag_to_sql(
    plan: LogicalPlan, config: PackageConfig, guard_empty: bool = True
) -> SqlSelect:
    # A named window answers each of its periods, filled over the combined answer: a
    # distribution reads NULL in a period with no rows. Its per-entity values are never filled,
    # which would put every entity in every period as a 0, so an explicit fill refuses.
    dense = bool(plan.time and plan.time.get("fill"))
    if dense and not _window_is_named(plan.time):
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED",
            "time.fill isn't supported for distributions; query without fill (periods with no "
            "data are omitted).",
        )
    if dense and guard_empty and zero_outputs(plan, config):
        # Filled after the combine, a sum's empty period can't tell 0 from no data yet.
        raise SemanticLayerError(
            "REWRITE_NOT_SUPPORTED",
            "A distribution beside a sum or count can't answer every period of a named window "
            "yet. Ask for the distribution and the sum or count in separate calls.",
            details={"reason": "distribution_beside_additive_in_named_window"},
        )
    branch_plan = replace(plan, time={**plan.time, "fill": False}) if dense else plan
    key_aliases = _query_key_aliases(plan)
    zero = zero_outputs(plan, config) if guard_empty else {}
    # Each branch settles its own groups: a NULL there is a group whose rows are all unknown.
    # A guarded branch marks the groups it has a row for, so the combine fills only the groups
    # it has none for.
    taken = {name.casefold() for name in [*key_aliases, *plan.post_aggregation_exprs]}
    present: dict[str, str] = {}
    branch_ctes: list[SqlCte] = []
    output_aliases: list[str] = []
    for index, (alias, expr_payload) in enumerate(plan.post_aggregation_exprs.items(), start=1):
        expr = _parse_public_expr(expr_payload)
        branch_name = f"agent_branch_{index}"
        # A read inside the branch answers for this output only.
        with owning(alias):
            branch_query = _single_expression_branch_select(
                expr, alias=alias, plan=branch_plan, config=config
            )
        if alias in zero:
            present[alias] = _unused_name(f"{branch_name}__present", taken)
            marker = SqlField(SqlLiteral(1), present[alias])
            branch_query = replace(branch_query, select=[*branch_query.select, marker])
        record_output_reads(alias, {alias})
        branch_ctes.append(
            SqlCte(name=branch_name, query=_namespace_sql_select(branch_query, f"{branch_name}__"))
        )
        output_aliases.append(alias)

    if not branch_ctes:
        raise SemanticLayerError(
            "INVALID_QUERY", "Agent DAG lowering requires at least one projected expression"
        )

    dialect = _dialect(config)
    # Branches disagree on the period's type: a dense branch (rolling, prior_period) reads it
    # from a calendar's DATE column, the others from DATE_TRUNC's timestamp. Postgres and
    # BigQuery join keys by their text, where the two never match, so every period came back
    # twice. Combine and return the period as the timestamp DATE_TRUNC gives.
    period_key = key_aliases[-1] if plan.time and plan.time.get("grain") else None

    def _key(side: str, key: str) -> Any:
        ref = SqlIdentifier(parts=[side, key])
        return SqlCast(ref, dialect.timestamp_type_name()) if key == period_key else ref

    combined_name = branch_ctes[0].name
    combine_ctes: list[SqlCte] = []
    available_aliases = [output_aliases[0], *_present(output_aliases[0], present)]
    for index, branch in enumerate(branch_ctes[1:], start=2):
        output = output_aliases[index - 1]
        left_alias = "left_side"
        right_alias = "right_side"
        next_name = f"agent_combined_{index}"
        select_fields = [
            SqlField(SqlCall("COALESCE", [_key(left_alias, key), _key(right_alias, key)]), key)
            for key in key_aliases
        ]
        select_fields.extend(
            SqlField(SqlIdentifier(parts=[left_alias, item]), item) for item in available_aliases
        )
        select_fields.extend(
            SqlField(SqlIdentifier(parts=[right_alias, item]), item)
            for item in [output, *_present(output, present)]
        )
        combine_ctes.append(
            SqlCte(
                name=next_name,
                query=SqlSelect(
                    select=select_fields,
                    from_table=SqlTableRef(name=combined_name, alias=left_alias),
                    joins=[
                        SqlJoin(
                            join_type="FULL OUTER",
                            table=SqlTableRef(name=branch.name, alias=right_alias),
                            on=_and_conditions(
                                [
                                    dialect.null_safe_eq(
                                        _key(left_alias, key), _key(right_alias, key)
                                    )
                                    for key in key_aliases
                                ]
                                or [SqlLiteral(True)]
                            ),
                        )
                    ],
                ),
            )
        )
        combined_name = next_name
        available_aliases.extend([output, *_present(output, present)])

    if dense:
        combined = SqlSelect(
            select=[
                SqlField(SqlIdentifier(parts=["base", alias]), alias)
                for alias in [*key_aliases, *output_aliases]
            ],
            from_table=SqlTableRef(name=combined_name, alias="base"),
        )
        combine_ctes.append(SqlCte(name="leaf_base", query=combined))
        combine_ctes.extend(
            _series_base_ctes(
                plan,
                config,
                _calendar_fill_binding(plan, config),
                key_aliases[-1],
                key_aliases[:-1],
                output_aliases,
            )
        )
        combined_name = "series_base"

    # A branch that has no row for a group leaves its output NULL in the combine: settle
    # those outputs again over the combined result, the same way each branch did. Its marker
    # is NULL there and 1 where the branch settled the group itself.
    guard_ctes: list[SqlCte] = []
    if zero:
        guard_ctes.extend(
            guard_empty_groups(combined_name, key_aliases, output_aliases, zero, rows=present)
        )
        combined_name = GUARDED_BASE

    final_source = "agent_projected"
    projected = SqlSelect(
        ctes=[*branch_ctes, *combine_ctes, *guard_ctes],
        select=[
            *[SqlField(SqlIdentifier(parts=["base", key]), key) for key in key_aliases],
            *[SqlField(SqlIdentifier(parts=["base", alias]), alias) for alias in output_aliases],
        ],
        from_table=SqlTableRef(name=combined_name, alias="base"),
    )
    if guard_empty:
        refuse_unsettled(projected, plan, config, combined=True)

    return SqlSelect(
        ctes=[SqlCte(name=final_source, query=projected)],
        select=[
            *[SqlField(SqlIdentifier(parts=[final_source, key]), key) for key in key_aliases],
            *[
                SqlField(SqlIdentifier(parts=[final_source, alias]), alias)
                for alias in output_aliases
            ],
        ],
        from_table=SqlTableRef(name=final_source),
        order_by=_requested_order(plan, final_source),
        limit=plan.query.get("limit"),
    )


def _present(output: str, present: dict[str, str]) -> list[str]:
    """The output's branch marker, if its branch has one."""
    return [present[output]] if output in present else []


def _unused_name(name: str, taken: set[str]) -> str:
    """``name``, or it with a suffix, until it takes no name in ``taken`` (case-folded); then
    taken too."""
    candidate, suffix = name, 2
    while candidate.casefold() in taken:
        candidate, suffix = f"{name}_{suffix}", suffix + 1
    taken.add(candidate.casefold())
    return candidate
