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

from .ast import normalize_query, time_output_alias
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
from .compiler_parts.paths import inner_lookups
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
from .compiler_parts.sql_lowering import lower_to_sql
from .compiler_parts.temporal import _requires_query_time as _requires_query_time
from .compiler_parts.temporal import _time_bound_relationship_ids as _time_bound_relationship_ids
from .diagnostics import relationship_contract_payload
from .dialects import dialect_for_warehouse
from .ir import ExplainArtifact, LogicalPlan
from .registry import Registry
from .relation_pipelines import attach_relation_ctes
from .renderer import render_select_for_profile
from .row_filters import RowFilter
from .schema import PackageConfig
from .sql_ast import SqlSelect
from .sql_preparation import finalize_parameters
from .temporal_support import validate_temporal_support


def _compile_query_sql_ast(
    config: PackageConfig,
    payload: dict[str, Any],
    *,
    project_cut: bool = False,
    guard_empty: bool = True,
) -> SqlSelect:
    """Compile a nested query; ``guard_empty=False`` for a distribution's per-entity values."""
    validate_temporal_support(config, payload)
    with candidate_planning():
        plan = _plan_query(
            config, None, normalize_query(payload, config=config), collapse_window=False
        )
    config = resolve_compile_config(plan, config)
    with plan_bindings(plan, project_cut=project_cut) as leaves:
        _record_bound_plan(plan, config, leaves.leaves)
        from .top_n import limit_order

        select = attach_relation_ctes(
            config, lower_to_sql(plan, config, guard_empty=guard_empty, default_order=False)
        )
        return limit_order(select, _tie_breakers(plan))[0]


def _tie_breakers(plan: LogicalPlan) -> list[str]:
    """The columns that break ties at a ``limit``: the group_by columns, then the period."""
    return [*plan.group_by, *([time_output_alias(plan.time)] if plan.time else [])]


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
        guard_empty: bool = True,
    ) -> SqlSelect:
        return _compile_query_sql_ast(
            config, payload, project_cut=project_cut, guard_empty=guard_empty
        )

    def predicate_source_sql(self, config: PackageConfig, payload: dict[str, Any]) -> SqlSelect:
        return _compile_predicate_source_ast(config, payload)


nested.register(_NestedCompiles())


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
    from .top_n import limit_order

    sql_ast, limit_order_keys = limit_order(bound.sql_ast, _tie_breakers(plan))
    outputs = {field.alias for field in sql_ast.select}
    from .compiler_parts.row_counts import override_row_counts, share_group_counts

    # The hidden columns the runtime reads and removes: each re-aggregated measure's row count,
    # and the rows each share's total spans.
    override_rows, share_groups = (
        {alias: column for alias, column in names.items() if column in outputs}
        for names in (override_row_counts(plan, config), share_group_counts(plan, config))
    )
    dialect = dialect_for_warehouse(config.package.warehouse)
    rendered = render_select_for_profile(
        sql_ast,
        str(payload.get("sql_profile", "audit") or "audit"),
        dialect=dialect,
    )
    prepared = replace(dialect.prepare_query(rendered), parameters=bound.parameters)
    prepared = finalize_parameters(prepared, config.package.connection.kind)
    limit_probe = None
    if limit_order_keys and sql_ast.limit is not None and sql_ast.limit > 0:
        probe_sql = render_select_for_profile(
            replace(sql_ast, limit=sql_ast.limit + 1),
            str(payload.get("sql_profile", "audit") or "audit"),
            dialect=dialect,
        )
        limit_probe = finalize_parameters(
            replace(dialect.prepare_query(probe_sql), parameters=bound.parameters),
            config.package.connection.kind,
        )
    rendered = prepared.sql
    from .compiler_parts.physical_plan import build_performance_plan, build_physical_plan

    physical_plan = build_physical_plan(plan, config)
    performance_plan = build_performance_plan(
        plan, config, physical_plan, rendered, bound.rollup_scans, sql_ast
    )
    compile_stats = {
        "compile_ms": round((time.perf_counter() - started) * 1000, 3),
        "cache_hit": False,
        "dialect": dialect_for_warehouse(config.package.warehouse).name,
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
        "override_rows": override_rows,
        "share_groups": share_groups,
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
    }
