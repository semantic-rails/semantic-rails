"""Query IR -> logical plan -> SQL AST compilation pipeline.

The compiler takes a normalized ``NormalizedQuery``, resolves
measures/metrics/dimensions against the package config, validates path
safety (fanout, rollup, predicate scope), applies the registered
rewrites, and emits both a logical plan (:mod:`semantic_rails.ir`) and a
SQL AST (:mod:`semantic_rails.sql_ast`). Exposes :func:`compile_query`
(the runtime entry point). Each phase lives in its own
``semantic_rails/compiler_parts/`` module (planning, binding, plan
checks, predicates, conversion, SQL lowering); this module is the
orchestration layer and re-exports the names other modules import from
it.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import asdict, replace
from typing import Any

from .ast import normalize_query
from .compiler_parts import nested
from .compiler_parts.bind import _collect_conversion_exprs as _collect_conversion_exprs
from .compiler_parts.binding import BoundQuery, _record_bound_plan, bind_query
from .compiler_parts.binding import _guard_route_row_filters as _guard_route_row_filters
from .compiler_parts.binding import bind_metadata_objects as bind_metadata_objects
from .compiler_parts.binding import query_route_rows as query_route_rows
from .compiler_parts.binding import read_routes as read_routes
from .compiler_parts.conversion import _conversion_sources as _conversion_sources
from .compiler_parts.conversion import _conversion_supported as _conversion_supported
from .compiler_parts.dependencies import candidate_planning, plan_bindings
from .compiler_parts.empty_groups import recording_zero_outputs
from .compiler_parts.lowering_common import _query_key_aliases
from .compiler_parts.paths import inner_lookups
from .compiler_parts.physical_plan import build_performance_plan, build_physical_plan
from .compiler_parts.plan_checks import NonAdditiveRefusal as NonAdditiveRefusal
from .compiler_parts.plan_checks import (
    _object_default_query_temporal_role as _object_default_query_temporal_role,
)
from .compiler_parts.plan_checks import (
    _validate_measure_validity_windows as _validate_measure_validity_windows,
)
from .compiler_parts.plan_checks import observed_only_groups as observed_only_groups
from .compiler_parts.plan_checks import observed_periods_reason as observed_periods_reason
from .compiler_parts.planning import (
    _extend_config_with_synthetic_measures as _extend_config_with_synthetic_measures,
)
from .compiler_parts.planning import _plan_query, resolve_compile_config
from .compiler_parts.planning import plan_query as plan_query
from .compiler_parts.predicate import _entity_determines as _entity_determines
from .compiler_parts.predicate import (
    _predicate_context_entity_candidates as _predicate_context_entity_candidates,
)
from .compiler_parts.predicate import _reduced_context_entities as _reduced_context_entities
from .compiler_parts.predicate import (
    _refuse_overridden_predicate_input as _refuse_overridden_predicate_input,
)
from .compiler_parts.row_counts import override_row_counts, share_group_counts
from .compiler_parts.sql_lowering import lower_to_sql
from .compiler_parts.temporal import _requires_query_time as _requires_query_time
from .compiler_parts.temporal import _time_bound_relationship_ids as _time_bound_relationship_ids
from .compiler_parts.total_rows import call_rows, with_call_rows
from .diagnostics import relationship_contract_payload
from .dialects import dialect_for_warehouse
from .ir import ExplainArtifact
from .registry import Registry
from .relation_pipelines import attach_relation_ctes
from .renderer import collecting_slots, render_select_for_profile
from .row_filters import RowFilter, verify_row_filters
from .schema import PackageConfig
from .sql_ast import (
    SqlCte,
    SqlField,
    SqlIdentifier,
    SqlOrder,
    SqlSelect,
    SqlTableRef,
    build_filter_condition,
)
from .sql_preparation import PreparedQuery, finalize_parameters
from .temporal_support import validate_temporal_support
from .top_n import limit_order


def _compile_query_sql_ast(
    config: PackageConfig,
    payload: dict[str, Any],
    *,
    project_cut: bool = False,
    standalone: bool = False,
    entity_values: bool = False,
) -> SqlSelect:
    """Compile a nested query; ``standalone`` for a call of its own (a total's coarser call),
    planned and settled as the request would be. ``entity_values`` for a distribution's
    per-entity values, settled as the request would be where its leaves join, over the entities
    with rows only."""
    validate_temporal_support(config, payload)
    with candidate_planning():
        plan = _plan_query(
            config, None, normalize_query(payload, config=config), collapse_window=standalone
        )
    config = resolve_compile_config(plan, config)
    with plan_bindings(
        plan, project_cut=project_cut, standalone=standalone, entity_values=entity_values
    ) as leaves:
        _record_bound_plan(plan, config, leaves.leaves)
        select = attach_relation_ctes(config, lower_to_sql(plan, config, default_order=False))
        return limit_order(select, _query_key_aliases(plan))[0]


def _compile_predicate_source_ast(config: PackageConfig, payload: dict[str, Any]) -> SqlSelect:
    """The per-entity values a metric predicate reads, settled like any query's.

    An add or subtract settles each operand the way the guard does (0 where the measure has
    data in the predicate's scope), so a predicate and a projection of the same expression
    agree. An entity with no rows at all is absent from the source, and the anti-join reads it
    like the entities the source lists (``absent_entities_gate``). The value is internal, so it
    never becomes a ``NO_DATA_IN_SCOPE`` output. Its lookups join INNER (``inner_lookups``).
    """
    with recording_zero_outputs(), inner_lookups():
        return _compile_query_sql_ast(config, payload, project_cut=True)


class _NestedCompiles:
    """The compiler's nested compiles (``compiler_parts.nested``), read at call time."""

    def query_sql(
        self,
        config: PackageConfig,
        payload: dict[str, Any],
        *,
        project_cut: bool = False,
        standalone: bool = False,
        entity_values: bool = False,
    ) -> SqlSelect:
        return _compile_query_sql_ast(
            config,
            payload,
            project_cut=project_cut,
            standalone=standalone,
            entity_values=entity_values,
        )

    def predicate_source_sql(self, config: PackageConfig, payload: dict[str, Any]) -> SqlSelect:
        return _compile_predicate_source_ast(config, payload)


nested.register(_NestedCompiles())


def _prepare(
    select: SqlSelect, config: PackageConfig, sql_profile: str, visibility: Any
) -> PreparedQuery:
    """``select`` as the warehouse runs it, held to the call's row filters first."""
    if visibility is not None:
        verify_row_filters(select, visibility)
    dialect = dialect_for_warehouse(config.package.warehouse)
    # The slots of the very render that is executed, in the order its text holds them.
    with collecting_slots() as slots:
        rendered = render_select_for_profile(select, sql_profile, dialect=dialect)
    rendered, parameters = slots.read(rendered)
    prepared = replace(dialect.prepare_query(rendered), parameters=parameters)
    return finalize_parameters(prepared, config.package.connection.kind)


def value_page(
    compiled: dict[str, Any], config: PackageConfig, *, column: str, search: str, offset: int
) -> dict[str, Any]:
    """``compiled``, a call listing ``column``'s values under a limit, as one page of them: the
    known values that contain ``search`` (the ``contains`` word: literal, ignoring case), in
    ascending order, from ``offset`` on. The page filters the listed rows, after grouping, so a
    value matches by itself on every route (a call's ``where`` filters the rows a value comes
    from: across a to-many hop it is refused or keeps every value of a matching row), and the
    warehouse skips and limits matching values only.
    """
    statement = compiled["sql_ast"]
    own = call_rows(statement)
    name = "value_rows"
    while name in {cte.name for cte in own.ctes}:
        name = f"{name}_"
    key = SqlIdentifier(parts=[name, column])
    where = [build_filter_condition(key, "missing", False)]
    if search:
        where.append(build_filter_condition(key, "contains", search, path="search"))
    paged = SqlSelect(
        ctes=[*own.ctes, SqlCte(name, replace(own, ctes=[], order_by=[], limit=None))],
        select=[SqlField(SqlIdentifier(parts=[name, row.alias]), row.alias) for row in own.select],
        from_table=SqlTableRef(name),
        where=where,
        order_by=[SqlOrder(key, "ASC", nulls_last=True)],
        limit=own.limit,
        offset=offset or None,
    )
    sql_ast = with_call_rows(statement, paged)

    def prepare(select: SqlSelect) -> PreparedQuery:
        return _prepare(select, config, compiled["sql_profile"], compiled["row_filter_visibility"])

    prepared = prepare(sql_ast)
    probe = compiled["limit_probe"]
    if probe is not None and paged.limit is not None:
        probe = prepare(with_call_rows(statement, replace(paged, limit=paged.limit + 1)))
    return {
        **compiled,
        "sql_ast": sql_ast,
        "sql": prepared.sql,
        "prepared_query": prepared,
        "limit_probe": probe,
        "explain": replace(
            compiled["explain"], rendered_sql=prepared.sql, sql_plan=asdict(sql_ast)
        ),
    }


def compile_query(
    config: PackageConfig,
    registry: Registry | None,
    payload: dict[str, Any],
    *,
    binding: BoundQuery | None = None,
    row_filters: Sequence[RowFilter] = (),
) -> dict[str, Any]:
    started = time.perf_counter()
    validate_temporal_support(config, payload)
    if binding is not None and row_filters:
        raise ValueError("Pass row_filters to bind_query, not with an existing binding.")
    bound = (
        binding
        if binding is not None
        else bind_query(config, registry, payload, row_filters=row_filters)
    )
    plan, config = bound.plan, bound.config
    # The call's own rows take the limit's tie-break, under any total rows that follow them.
    own, limit_order_keys = limit_order(call_rows(bound.sql_ast), _query_key_aliases(plan))
    sql_ast = with_call_rows(bound.sql_ast, own)
    outputs = {field.alias for field in sql_ast.select}
    # The hidden columns the runtime reads and removes: each re-aggregated measure's row count,
    # the rows each share's total spans, and, by output, the columns marking the groups the
    # measures it reads never reached (``ZERO_FILLED_GROUPS``).
    override_rows, share_groups = (
        {alias: column for alias, column in names.items() if column in outputs}
        for names in (override_row_counts(plan, config), share_group_counts(plan, config))
    )
    absent_rows = {
        item["output"]: list(item["absent"])
        for item in bound.zero_outputs
        if item.get("absent") and outputs.issuperset(item["absent"])
    }
    dialect = dialect_for_warehouse(config.package.warehouse)
    sql_profile = str(payload.get("sql_profile", "audit") or "audit")

    def prepare(select: SqlSelect) -> PreparedQuery:
        return _prepare(select, config, sql_profile, bound.visibility)

    prepared = prepare(sql_ast)
    limit_probe = None
    if limit_order_keys and own.limit is not None and own.limit > 0:
        limit_probe = prepare(with_call_rows(sql_ast, replace(own, limit=own.limit + 1)))
    rendered = prepared.sql
    physical_plan = build_physical_plan(plan, config)
    performance_plan = build_performance_plan(
        plan, config, physical_plan, rendered, bound.rollup_scans, sql_ast
    )
    compile_stats = {
        "compile_ms": round((time.perf_counter() - started) * 1000, 3),
        "cache_hit": False,
        "dialect": dialect.name,
        "physical_plan_nodes": len(physical_plan.nodes),
    }
    relationships = {row.id: row for row in config.relationships}
    explain = ExplainArtifact(
        normalized_query=plan.query,
        alias_resolution=plan.resolved_ids,
        chosen_paths={
            entity: {
                "selected": path,
                "candidates": plan.candidate_paths.get(entity, []),
                "contracts": [
                    relationship_contract_payload(relationships[rel_id])
                    for rel_id in list(path or [])
                    if rel_id in relationships
                ],
            }
            for entity, path in plan.selected_paths.items()
        },
        grain_analysis=plan.grain_analysis,
        fanout_strategy=plan.fanout_strategy,
        rewrite_strategy={
            "status": "rewritten" if plan.rewrite_steps else "direct",
            "steps": [asdict(row) for row in plan.rewrite_steps],
        },
        logical_plan=asdict(plan),
        semantic_dag=asdict(plan.semantic_dag),
        physical_plan=asdict(physical_plan),
        performance_plan=asdict(performance_plan),
        sql_plan=asdict(sql_ast),
        rendered_sql=rendered,
        compile_stats=compile_stats,
    )
    return {
        "logical_plan": plan,
        "sql_ast": sql_ast,
        "sql": rendered,
        "prepared_query": prepared,
        "limit_probe": limit_probe,
        "limit_order_keys": limit_order_keys,
        "sql_profile": sql_profile,
        "override_rows": override_rows,
        "share_groups": share_groups,
        "absent_rows": absent_rows,
        "explain": explain,
        "physical_plan": physical_plan,
        "performance_plan": performance_plan,
        "compile_stats": compile_stats,
        "stock_key_gaps": list(bound.stock_key_gaps),
        "zero_outputs": list(bound.zero_outputs),
        "retained_filtered_series": list(bound.retained_filtered_series),
        "route_choices": list(bound.route_choices),
        "route_decisions": list(bound.route_decisions),
        "coverage_now": bound.coverage_now,
        "balance_edges": list(bound.balance_edges),
        "balance_coverage": list(bound.balance_coverage),
        "closing_reads": list(bound.closing_reads),
        # Every probe the runtime builds from this statement is held to it again.
        "row_filter_visibility": bound.visibility,
    }
