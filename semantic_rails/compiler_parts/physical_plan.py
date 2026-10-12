"""The physical and performance plans a compiled query explains: its scans, joins, filters and
the risks of reading them.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from ..acceleration.routing import (
    aggregate_routing_report,
)
from ..ast import (
    normalize_query,
)
from ..expressions import (
    expr_to_dict,
)
from ..ir import (
    LogicalPlan,
    PerformancePlan,
    PhysicalPlan,
    PhysicalPlanNode,
)
from ..schema import (
    PackageConfig,
)
from ..sql_ast import (
    SqlSelect,
    SqlTableRef,
)
from .bind import (
    _bound_filter_clauses,
    _expression_alias,
)
from .empty_groups import (
    sql_nodes,
)
from .indexes import (
    _dimension_index,
    _entity_index,
    _measure_index,
    _relationship_index,
)
from .leaves import _filters_pushable_to_entity_scan, _source_rollup_strategy
from .lowering_common import (
    _all_metric_predicates,
    _dialect,
    _entity_in_terms_of_anchor_plan,
    _measure_owned_relation,
    _measure_plan_groups,
    _plan_requires_agent_dag_lowering,
    _query_key_aliases,
    _query_metric_predicates,
    _selected_aggregate_relation,
)
from .paths import (
    _expression_root_entity,
)
from .predicate import _predicate_time_spec
from .predicate_sets import (
    _anchored_entity_set_plan,
    _predicate_expr_from_payload,
    _predicate_key_aliases,
    _predicate_physical_nodes,
)
from .time_series import _conversion_exprs_for_plan


def build_physical_plan(plan: LogicalPlan, config: PackageConfig) -> PhysicalPlan:
    entities = _entity_index(config)
    measures = _measure_index(config)
    relationships = _relationship_index(config)
    nodes: list[PhysicalPlanNode] = []
    outputs: dict[str, str] = {}
    optimizations: list[dict[str, Any]] = []
    key_aliases = _query_key_aliases(plan)

    anchored = _anchored_entity_set_plan(plan, config)
    if anchored is not None:
        measure = measures[anchored.denominator_measure_plan.bound_measure.measure_id]
        source_relation = _measure_owned_relation(measure, entities)
        scan_id = "anchor_scan"
        nodes.append(
            PhysicalPlanNode(
                id=scan_id,
                kind="Scan",
                label=f"Scan anchor relation {source_relation}",
                details={
                    "source_entity": measure.entity,
                    "relation": source_relation,
                    "selected_relation": source_relation,
                    "selected_relation_type": "raw",
                    "measures": [
                        {
                            "measure_id": anchored.denominator_measure_plan.bound_measure.measure_id,
                            "aggregation": anchored.denominator_measure_plan.bound_measure.aggregation,
                            "alias": anchored.denominator_measure_plan.bound_measure.alias,
                        }
                    ],
                },
            )
        )
        nodes.append(
            PhysicalPlanNode(
                id="anchor_filter",
                kind="Filter",
                inputs=[scan_id],
                details={
                    "query_filters": list(plan.query.get("where", []) or []),
                    "measure_filters": _bound_filter_clauses(
                        anchored.denominator_measure_plan.bound_measure, config
                    ),
                    "time_filter": dict(plan.time or {}),
                    "pushed_to_scan": True,
                },
            )
        )
        nodes.append(
            PhysicalPlanNode(
                id="snapshot_entity_set",
                kind="SnapshotEntitySet",
                inputs=["anchor_filter"],
                details={
                    "anchor_entity": anchored.anchor_entity,
                    "snapshot_policy": measure.accumulation.snapshot,
                    "strategy": "qualify_row_number"
                    if _dialect(config).capabilities().get("qualify")
                    else "aggregate_join",
                    "grain": list(key_aliases),
                },
            )
        )
        predicate_node_ids: list[str] = []
        for index, predicate_payload in enumerate(
            [*anchored.base_predicates, *anchored.extra_predicates]
        ):
            predicate = _predicate_expr_from_payload(predicate_payload)
            root_entity = _expression_root_entity(predicate.input, config)
            relation = entities[root_entity].table
            predicate_scan_id = f"predicate_{index + 1}_scan"
            predicate_id = f"predicate_set_{index + 1}"
            nodes.append(
                PhysicalPlanNode(
                    id=predicate_scan_id,
                    kind="Scan",
                    label=f"Scan predicate relation {relation}",
                    details={
                        "source_entity": root_entity,
                        "relation": relation,
                        "selected_relation": relation,
                        "selected_relation_type": "raw",
                        "measures": [
                            {
                                "predicate": expr_to_dict(predicate.input),
                                "op": predicate.op,
                                "value": predicate.value,
                            }
                        ],
                    },
                )
            )
            nodes.append(
                PhysicalPlanNode(
                    id=f"predicate_{index + 1}_filter",
                    kind="Filter",
                    inputs=[predicate_scan_id],
                    details={
                        "query_filters": [],
                        "measure_filters": [],
                        "time_filter": dict(
                            _predicate_time_spec(predicate, normalize_query(plan.query), config)
                            or {}
                        ),
                        "pushed_to_scan": True,
                    },
                )
            )
            nodes.append(
                PhysicalPlanNode(
                    id=predicate_id,
                    kind="PredicateSet",
                    inputs=[f"predicate_{index + 1}_filter"],
                    details={
                        "entity": predicate.entity,
                        "keys": _predicate_key_aliases(predicate, config),
                        "scope": "minimal_entity_period",
                        "dimension_joins_pruned": True,
                    },
                )
            )
            predicate_node_ids.append(predicate_id)
        if anchored.denominator_measure_plan.path_selections:
            nodes.append(
                PhysicalPlanNode(
                    id="dimension_decorate",
                    kind="DimensionDecorate",
                    inputs=["snapshot_entity_set"],
                    details={
                        "paths": [
                            asdict(selection)
                            for selection in anchored.denominator_measure_plan.path_selections
                        ],
                        "delayed_until_after_entity_set": True,
                    },
                )
            )
            aggregate_inputs = ["dimension_decorate", *predicate_node_ids]
        else:
            aggregate_inputs = ["snapshot_entity_set", *predicate_node_ids]
        nodes.append(
            PhysicalPlanNode(
                id="final_aggregate",
                kind="FinalAggregate",
                inputs=aggregate_inputs,
                details={
                    "anchor": anchored.anchor_entity,
                    "outputs": [anchored.output_alias],
                    "key_aliases": list(key_aliases),
                    "join_strategy": "anchor_left_join_predicate_sets",
                    "full_outer_alignments": 0,
                },
            )
        )
        optimizations.extend(
            [
                {
                    "kind": "anchored_entity_set",
                    "status": "applied",
                    "target": "final_aggregate",
                    "anchor_entity": anchored.anchor_entity,
                    "semantic_preconditions": {
                        "shared_denominator": True,
                        "predicate_entity": anchored.anchor_entity,
                        "aggregation": anchored.denominator_measure_plan.bound_measure.aggregation,
                    },
                },
                {
                    "kind": "predicate_dimension_pruning",
                    "status": "applied",
                    "target": "predicate_sets",
                },
                {
                    "kind": "snapshot_row_selection",
                    "status": "applied",
                    "target": "snapshot_entity_set",
                    "strategy": "qualify_row_number"
                    if _dialect(config).capabilities().get("qualify")
                    else "aggregate_join",
                },
            ]
        )
        return PhysicalPlan(
            version=1,
            nodes=nodes,
            outputs={anchored.output_alias: "final_aggregate"},
            optimizations=optimizations,
        )

    if _plan_requires_agent_dag_lowering(plan, config):
        nodes.append(
            PhysicalPlanNode(
                id="agent_dag",
                kind="FinalProject",
                label="Agent DAG branch lowering",
                details={
                    "lowering": "branch_queries",
                    "reason": "distribution_or_entity_value_expression",
                    "semantic_dag_nodes": [node.kind for node in plan.semantic_dag.nodes],
                },
            )
        )
        return PhysicalPlan(
            version=1,
            nodes=nodes,
            outputs={alias: "agent_dag" for alias in plan.post_aggregation_exprs},
            optimizations=optimizations,
        )

    conversion_exprs = _conversion_exprs_for_plan(plan, config)
    if conversion_exprs and not plan.measure_plans:
        query_predicates = _query_metric_predicates(plan)
        conversion_node_ids: list[str] = []
        for index, expr in enumerate(conversion_exprs, start=1):
            base_entity = _expression_root_entity(expr.base, config)
            converted_entity = _expression_root_entity(expr.converted, config)
            base_scan_id = f"conversion_{index}_base_scan"
            converted_scan_id = f"conversion_{index}_converted_scan"
            match_id = f"conversion_{index}_match"
            aggregate_id = f"conversion_{index}_aggregate"
            nodes.append(
                PhysicalPlanNode(
                    id=base_scan_id,
                    kind="Scan",
                    label=f"Scan conversion base relation {entities[base_entity].table}",
                    details={
                        "source_entity": base_entity,
                        "relation": entities[base_entity].table,
                        "selected_relation": entities[base_entity].table,
                        "selected_relation_type": "raw",
                        "measures": [{"conversion_base": expr_to_dict(expr.base)}],
                    },
                )
            )
            nodes.append(
                PhysicalPlanNode(
                    id=converted_scan_id,
                    kind="Scan",
                    label=f"Scan conversion converted relation {entities[converted_entity].table}",
                    details={
                        "source_entity": converted_entity,
                        "relation": entities[converted_entity].table,
                        "selected_relation": entities[converted_entity].table,
                        "selected_relation_type": "raw",
                        "measures": [{"conversion_converted": expr_to_dict(expr.converted)}],
                    },
                )
            )
            predicate_nodes, predicate_set_ids = _predicate_physical_nodes(
                query_predicates,
                prefix=f"conversion_{index}",
                plan=plan,
                config=config,
            )
            nodes.extend(predicate_nodes)
            nodes.append(
                PhysicalPlanNode(
                    id=match_id,
                    kind="ConversionMatch",
                    inputs=[base_scan_id, converted_scan_id, *predicate_set_ids],
                    details={
                        "entity": expr.entity,
                        "window": {"unit": expr.window_unit, "value": expr.window_value},
                        "matching_mode": expr.matching_mode,
                        "constant_properties": list(expr.constant_properties or []),
                        "metric_predicate_filters": [
                            expr_to_dict(predicate) for predicate in query_predicates
                        ],
                        "predicate_sets_joined_to_base": list(predicate_set_ids),
                    },
                )
            )
            nodes.append(
                PhysicalPlanNode(
                    id=aggregate_id,
                    kind="ConversionAggregate",
                    inputs=[match_id],
                    details={
                        "grain": list(key_aliases),
                        "output_alias": _expression_alias(expr, config),
                    },
                )
            )
            conversion_node_ids.append(aggregate_id)
            if predicate_set_ids:
                optimizations.append(
                    {
                        "kind": "conversion_metric_predicate_pushdown",
                        "status": "applied",
                        "target": match_id,
                        "predicate_sets": list(predicate_set_ids),
                    }
                )
        if len(conversion_node_ids) > 1:
            nodes.append(
                PhysicalPlanNode(
                    id="align_conversions",
                    kind="AlignFacts",
                    inputs=conversion_node_ids,
                    details={
                        "keys": list(key_aliases),
                        "join_type": "FULL OUTER",
                        "reason": "multiple_independent_conversion_leaves",
                    },
                )
            )
            final_inputs = ["align_conversions"]
        else:
            final_inputs = list(conversion_node_ids)
        nodes.append(
            PhysicalPlanNode(
                id="final_project",
                kind="FinalProject",
                inputs=final_inputs,
                details={
                    "outputs": list(plan.post_aggregation_exprs),
                    "key_aliases": list(key_aliases),
                    "projection_wrappers": "pruned_when_no_metric_filters_or_order_by",
                },
            )
        )
        optimizations.append(
            {"kind": "conversion_leaf_lowering", "status": "applied", "target": "final_project"}
        )
        return PhysicalPlan(
            version=1,
            nodes=nodes,
            outputs={alias: "final_project" for alias in plan.post_aggregation_exprs},
            optimizations=optimizations,
        )

    if not plan.measure_plans:
        nodes.append(
            PhysicalPlanNode(
                id="distinct_scan",
                kind="Scan",
                label="Distinct value scan",
                details={
                    "root_entity": plan.root_entity,
                    "relation": entities[plan.root_entity].table,
                    "group_by": list(plan.group_by),
                },
            )
        )
        nodes.append(
            PhysicalPlanNode(
                id="final_project",
                kind="FinalProject",
                inputs=["distinct_scan"],
                details={"outputs": list(plan.post_aggregation_exprs)},
            )
        )
        return PhysicalPlan(
            version=1,
            nodes=nodes,
            outputs={alias: "final_project" for alias in plan.post_aggregation_exprs},
            optimizations=optimizations,
        )

    aggregate_node_ids: list[str] = []
    for group in _measure_plan_groups(plan, config):
        first_plan = group[0]
        measure = measures[first_plan.bound_measure.measure_id]
        selected_aggregate = _selected_aggregate_relation(first_plan, config)
        fanout_dedup = first_plan.rewrite_strategy == "fanout_dedup"
        entity_terms_anchor = (
            None if fanout_dedup else _entity_in_terms_of_anchor_plan(plan, first_plan, config)
        )
        scan_entity = (
            str(entity_terms_anchor["anchor_entity"])
            if entity_terms_anchor is not None and selected_aggregate is None
            else measure.entity
        )
        scan_relation = (
            selected_aggregate.relation
            if selected_aggregate is not None
            else (
                entities[scan_entity].table
                if entity_terms_anchor is not None
                else _measure_owned_relation(measure, entities)
            )
        )
        scan_id = f"{first_plan.cte_name}_scan"
        filter_id = f"{first_plan.cte_name}_filter"
        join_id = f"{first_plan.cte_name}_joins"
        aggregate_id = first_plan.cte_name
        measure_payloads = [
            {
                "measure_id": item.bound_measure.measure_id,
                "aggregation": item.bound_measure.aggregation,
                "alias": item.bound_measure.alias,
            }
            for item in group
        ]
        nodes.append(
            PhysicalPlanNode(
                id=scan_id,
                kind="Scan",
                label=f"Scan {scan_relation}",
                details={
                    "source_entity": measure.entity,
                    "physical_anchor_entity": scan_entity,
                    "omitted_semantic_root_entity": measure.entity
                    if entity_terms_anchor is not None and scan_entity != measure.entity
                    else "",
                    "relation": scan_relation,
                    "selected_relation": scan_relation,
                    "selected_relation_type": "aggregate_relation"
                    if selected_aggregate is not None
                    else "raw",
                    "aggregate_relation_id": selected_aggregate.id
                    if selected_aggregate is not None
                    else "",
                    "semantic_equivalence": selected_aggregate.equivalence_kind
                    if selected_aggregate is not None
                    else "",
                    "time_grain": selected_aggregate.grain
                    if selected_aggregate is not None
                    else "",
                    "measures": measure_payloads,
                },
            )
        )
        filter_details = {
            "query_filters": list(plan.query.get("where", []) or []),
            "measure_filters": _bound_filter_clauses(first_plan.bound_measure, config),
            "time_filter": dict(plan.time or {}),
            "pushed_to_scan": selected_aggregate is not None
            or entity_terms_anchor is None
            or _filters_pushable_to_entity_scan(scan_entity, plan, first_plan, config),
        }
        has_filter = bool(
            filter_details["query_filters"]
            or filter_details["measure_filters"]
            or filter_details["time_filter"]
        )
        if has_filter and filter_details["pushed_to_scan"]:
            nodes.append(
                PhysicalPlanNode(
                    id=filter_id, kind="Filter", inputs=[scan_id], details=filter_details
                )
            )
            input_id = filter_id
            optimizations.append(
                {"kind": "filter_pushdown", "status": "applied", "target": scan_id}
            )
        else:
            input_id = scan_id
        display_path_selections = (
            []
            if selected_aggregate is not None
            else (
                list(entity_terms_anchor["path_selections"])
                if entity_terms_anchor is not None
                else list(first_plan.path_selections)
            )
        )
        if display_path_selections:
            contract_payloads = []
            for selection in display_path_selections:
                for rel_id in selection.chosen_path:
                    rel = relationships.get(rel_id)
                    if rel is not None:
                        contract_payloads.append(
                            {
                                "relationship_id": rel.id,
                                "cardinality": rel.cardinality,
                                "safety": rel.safety,
                                "temporal_validity": bool(rel.temporal_validity),
                            }
                        )
            nodes.append(
                PhysicalPlanNode(
                    id=join_id,
                    kind="Join",
                    inputs=[input_id],
                    details={
                        "paths": [asdict(selection) for selection in display_path_selections],
                        "contracts": contract_payloads,
                        "rewrite_reason": "entity_in_terms_of"
                        if entity_terms_anchor is not None
                        else ("fanout_dedup" if fanout_dedup else ""),
                        "physical_anchor_entity": scan_entity
                        if entity_terms_anchor is not None
                        else "",
                        "omitted_semantic_root_entity": str(
                            entity_terms_anchor["omitted_root_entity"]
                        )
                        if entity_terms_anchor is not None
                        else "",
                        "linked_dimensions": [
                            {
                                "dimension_id": dim_id,
                                "target_entity": _dimension_index(config)[dim_id].entity,
                                "physical_anchor_entity": scan_entity,
                                "rewrite_reason": "entity_in_terms_of",
                            }
                            for dim_id in plan.group_by
                        ]
                        if entity_terms_anchor is not None
                        else [],
                    },
                )
            )
            input_id = join_id
        if has_filter and not filter_details["pushed_to_scan"]:
            nodes.append(
                PhysicalPlanNode(
                    id=filter_id, kind="Filter", inputs=[input_id], details=filter_details
                )
            )
            input_id = filter_id
        metric_predicates = _all_metric_predicates(plan, first_plan)
        if metric_predicates:
            predicate_nodes, predicate_set_ids = _predicate_physical_nodes(
                metric_predicates,
                prefix=aggregate_id,
                plan=plan,
                config=config,
            )
            nodes.extend(predicate_nodes)
            predicate_join_id = f"{aggregate_id}_predicate_joins"
            nodes.append(
                PhysicalPlanNode(
                    id=predicate_join_id,
                    kind="Join",
                    inputs=[input_id, *predicate_set_ids],
                    details={
                        "join_type": "INNER",
                        "reason": "metric_predicate_entity_sets",
                        "predicate_sets": list(predicate_set_ids),
                        "predicates": [expr_to_dict(predicate) for predicate in metric_predicates],
                        "contracts": [],
                    },
                )
            )
            input_id = predicate_join_id
            optimizations.append(
                {
                    "kind": "metric_predicate_source_accounting",
                    "status": "applied",
                    "target": predicate_join_id,
                    "predicate_sets": list(predicate_set_ids),
                }
            )
        rollup_strategy = (
            {
                "strategy": "aggregate_relation",
                "reason": "selected_lossless_aggregate_relation",
                "aggregate_relation_id": selected_aggregate.id,
            }
            if selected_aggregate is not None
            else _source_rollup_strategy(plan, first_plan, config)
        )
        if len(group) > 1:
            optimizations.append(
                {
                    "kind": "same_source_measure_folding",
                    "status": "applied",
                    "target": aggregate_id,
                    "measure_aliases": [item["alias"] for item in measure_payloads],
                }
            )
        optimizations.append(
            {
                "kind": "source_aggregate_placement",
                "status": rollup_strategy["strategy"],
                "target": aggregate_id,
                "reason": rollup_strategy["reason"],
            }
        )
        nodes.append(
            PhysicalPlanNode(
                id=aggregate_id,
                kind="Aggregate",
                inputs=[input_id],
                details={
                    "grain": list(key_aliases),
                    "measures": measure_payloads,
                    "folded_measure_count": len(group),
                    "aggregate_placement": rollup_strategy,
                    "aggregate_relation_id": selected_aggregate.id
                    if selected_aggregate is not None
                    else "",
                },
            )
        )
        aggregate_node_ids.append(aggregate_id)
        for item in group:
            outputs[item.bound_measure.alias] = aggregate_id

    if len(aggregate_node_ids) > 1:
        nodes.append(
            PhysicalPlanNode(
                id="align_facts",
                kind="AlignFacts",
                inputs=aggregate_node_ids,
                details={
                    "keys": list(key_aliases),
                    "join_type": "FULL OUTER",
                    "reason": "multiple_independent_metric_leaves",
                },
            )
        )
        final_inputs = ["align_facts"]
    else:
        final_inputs = list(aggregate_node_ids)

    nodes.append(
        PhysicalPlanNode(
            id="final_project",
            kind="FinalProject",
            inputs=final_inputs,
            details={
                "outputs": list(plan.post_aggregation_exprs),
                "key_aliases": list(key_aliases),
                "projection_wrappers": "pruned_when_no_metric_filters_or_order_by",
            },
        )
    )
    for alias in plan.post_aggregation_exprs:
        outputs[alias] = "final_project"
    return PhysicalPlan(version=1, nodes=nodes, outputs=outputs, optimizations=optimizations)


_LARGE_PRIMITIVE_RELATION_HINTS = {
    "COHORTED_EMAILS",
    "COHORTED_SMS_DETAIL",
    "COHORTED_PUSH",
    "COHORTED_MESSAGES",
    "RPT_DAILY_ACCOUNT_PRODUCT_SUBSCRIPTIONS_METRICS_WIDE",
    "GMV_FEED_POWDERHORN",
}


def _physical_node_index(physical_plan: PhysicalPlan) -> dict[str, PhysicalPlanNode]:
    return {node.id: node for node in physical_plan.nodes}


def _upstream_nodes(
    node_id: str, nodes: dict[str, PhysicalPlanNode], *, seen: set[str] | None = None
) -> list[PhysicalPlanNode]:
    seen = set(seen or set())
    if node_id in seen:
        return []
    seen.add(node_id)
    node = nodes.get(node_id)
    if node is None:
        return []
    out = [node]
    for input_id in list(node.inputs or []):
        out.extend(_upstream_nodes(input_id, nodes, seen=seen))
    return out


def _time_window_days(time_spec: dict[str, Any]) -> int | None:
    start = time_spec.get("start")
    end = time_spec.get("end")
    if start is None or end is None:
        return None

    def _parse(value: Any) -> datetime:
        text = str(value)
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return datetime.fromisoformat(text[:10])

    try:
        return max(0, (_parse(end) - _parse(start)).days)
    except Exception:
        return None


def _relation_has_large_hint(relation: str) -> bool:
    normalized = relation.upper()
    return any(hint in normalized for hint in _LARGE_PRIMITIVE_RELATION_HINTS)


def _path_hop_count(join_node: PhysicalPlanNode) -> int:
    max_hops = 0
    for selection in list(join_node.details.get("paths", []) or []):
        max_hops = max(max_hops, len(list(selection.get("chosen_path", []) or [])))
    return max_hops


def _join_payload(
    aggregate_node: PhysicalPlanNode, join_node: PhysicalPlanNode, strategy: dict[str, Any]
) -> dict[str, Any]:
    contracts = list(join_node.details.get("contracts", []) or [])
    safe_contracts = bool(contracts) and all(
        str(row.get("safety", "safe")).lower() == "safe"
        and str(row.get("cardinality", "")) in {"1:1", "N:1"}
        for row in contracts
    )
    return {
        "aggregate_id": aggregate_node.id,
        "join_node": join_node.id,
        "strategy": str(strategy.get("strategy", "")),
        "reason": str(strategy.get("reason", "")),
        "max_path_hops": _path_hop_count(join_node),
        "safe_before_aggregate": safe_contracts,
        "paths": list(join_node.details.get("paths", []) or []),
        "contracts": contracts,
    }


def build_performance_plan(
    plan: LogicalPlan,
    config: PackageConfig,
    physical_plan: PhysicalPlan,
    rendered_sql: str,
    rollup_scans: frozenset[str],
    sql_ast: SqlSelect,
) -> PerformancePlan:
    nodes = _physical_node_index(physical_plan)
    scans = [node for node in physical_plan.nodes if node.kind == "Scan"]
    filters_by_scan = {
        str(node.inputs[0]): node
        for node in physical_plan.nodes
        if node.kind == "Filter" and node.inputs
    }
    estimated_scan_relations: list[dict[str, Any]] = []
    date_filter_pushdown: list[dict[str, Any]] = []
    for scan in scans:
        details = dict(scan.details or {})
        relation = str(details.get("selected_relation") or details.get("relation") or "")
        filter_node = filters_by_scan.get(scan.id)
        time_filter = dict(
            (filter_node.details if filter_node else {}).get("time_filter", {}) or {}
        )
        has_start = time_filter.get("start") is not None
        has_end = time_filter.get("end") is not None
        pushed = bool(filter_node and filter_node.details.get("pushed_to_scan"))
        scan_payload = {
            "scan_id": scan.id,
            "relation": relation,
            "source_entity": details.get("source_entity") or details.get("root_entity") or "",
            "selected_relation_type": str(details.get("selected_relation_type", "raw") or "raw"),
            "measures": list(details.get("measures", []) or []),
            "estimated_rows": "unknown_compile_only",
            "has_time_filter": bool(time_filter),
            "time_filter_has_start": has_start,
            "time_filter_has_end": has_end,
            "has_source_filter": bool(
                (filter_node.details if filter_node else {}).get("query_filters")
                or (filter_node.details if filter_node else {}).get("measure_filters")
            ),
            "large_relation_hint": _relation_has_large_hint(relation),
        }
        estimated_scan_relations.append(scan_payload)
        date_filter_pushdown.append(
            {
                "scan_id": scan.id,
                "relation": relation,
                "pushed_to_scan": pushed,
                "has_time_filter": bool(time_filter),
                "has_start": has_start,
                "has_end": has_end,
                "time_filter": time_filter,
            }
        )

    # These reads deliberately omit the request window. Count the emitted AST nodes,
    # including nested probes, rather than inferring them from the logical leaves.
    for read in sql_nodes(sql_ast):
        if not isinstance(read, SqlSelect) or not read.observation_scan:
            continue
        if not isinstance(read.from_table, SqlTableRef):
            continue
        relation = read.from_table.name
        scan_id = f"scope_scan_{len(estimated_scan_relations) + 1}"
        estimated_scan_relations.append(
            {
                "scan_id": scan_id,
                "relation": relation,
                "source_entity": next((e.id for e in config.entities if e.table == relation), ""),
                "selected_relation_type": "raw",
                "measures": [],
                "estimated_rows": "unknown_compile_only",
                "has_time_filter": False,
                "time_filter_has_start": False,
                "time_filter_has_end": False,
                "has_source_filter": bool(read.where),
                "large_relation_hint": _relation_has_large_hint(relation),
            }
        )
        date_filter_pushdown.append(
            {
                "scan_id": scan_id,
                "relation": relation,
                "pushed_to_scan": bool(read.where),
                "has_time_filter": False,
                "has_start": False,
                "has_end": False,
                "time_filter": {},
            }
        )

    pre_aggregation_grain: dict[str, list[str]] = {}
    joins_before_aggregate: list[dict[str, Any]] = []
    joins_after_aggregate: list[dict[str, Any]] = []
    for aggregate in [node for node in physical_plan.nodes if node.kind == "Aggregate"]:
        pre_aggregation_grain[aggregate.id] = list(aggregate.details.get("grain", []) or [])
        strategy = dict(aggregate.details.get("aggregate_placement", {}) or {})
        upstream_joins = [
            node for node in _upstream_nodes(aggregate.id, nodes) if node.kind == "Join"
        ]
        for join_node in upstream_joins:
            payload = _join_payload(aggregate, join_node, strategy)
            if strategy.get("strategy") == "source_preaggregate":
                joins_after_aggregate.append(payload)
            else:
                joins_before_aggregate.append(payload)

    full_outer_alignments = 0
    for align in [node for node in physical_plan.nodes if node.kind == "AlignFacts"]:
        full_outer_alignments += max(1, len(list(align.inputs or [])) - 1)
    if full_outer_alignments == 0 and rendered_sql:
        full_outer_alignments = rendered_sql.upper().count("FULL OUTER JOIN")

    raw_scans = [row for row in estimated_scan_relations if row["selected_relation_type"] == "raw"]
    raw_fact_count = sum(
        scan.details.get("selected_relation_type", "raw") == "raw" for scan in scans
    )
    bounded_raw_scans = [
        row
        for row in date_filter_pushdown
        if any(
            scan["scan_id"] == row["scan_id"] and scan["selected_relation_type"] == "raw"
            for scan in estimated_scan_relations
        )
        and row["has_start"]
        and row["has_end"]
    ]
    all_raw_bounded = bool(raw_scans) and len(bounded_raw_scans) == len(raw_scans)
    window_days = _time_window_days(dict(plan.time or {}))
    large_raw_relations = [row["relation"] for row in raw_scans if row.get("large_relation_hint")]
    multi_hop_after_aggregate = any(
        int(row.get("max_path_hops", 0) or 0) > 1 for row in joins_after_aggregate
    )
    source_preaggregates = [
        node.id
        for node in physical_plan.nodes
        if node.kind == "Aggregate"
        and dict(node.details.get("aggregate_placement", {}) or {}).get("strategy")
        == "source_preaggregate"
    ]
    multi_fact_raw_alignment = raw_fact_count >= 2 and full_outer_alignments > 0
    expensive_parent_or_multihop = multi_hop_after_aggregate or any(
        "parent" in dim.lower() or "geo" in dim.lower() for dim in plan.group_by
    )
    q3_like_primitive_shape = (
        raw_fact_count >= 3 and full_outer_alignments >= 2 and expensive_parent_or_multihop
    )

    risk_reasons: list[str] = []
    if raw_scans and not all_raw_bounded:
        risk_reasons.append("unbounded_raw_primitive_scan")
    if large_raw_relations:
        risk_reasons.append("known_large_primitive_relation")
    if multi_fact_raw_alignment:
        risk_reasons.append("multi_fact_raw_alignment")
    if source_preaggregates:
        risk_reasons.append("requires_source_preaggregation_before_dimension_joins")
    if q3_like_primitive_shape:
        risk_reasons.append("primitive_exact_but_expensive")

    risk_level = "low"
    execution_recommendation = "safe_to_run"
    if (
        raw_scans
        and not all_raw_bounded
        and (large_raw_relations or raw_fact_count >= 2)
        or q3_like_primitive_shape
        and (window_days is None or window_days > 45)
    ):
        risk_level = "high"
        execution_recommendation = "needs_acceleration_layer"
    elif multi_fact_raw_alignment or source_preaggregates or large_raw_relations:
        risk_level = "medium"
        execution_recommendation = "run_with_small_window"

    notes: list[str] = []
    if q3_like_primitive_shape:
        notes.append("primitive_exact_but_expensive")
    if source_preaggregates:
        notes.append(
            "source filters are pushed before primitive pre-aggregation; dimension joins occur after source rollup"
        )
    if not scans and any(node.kind == "DistributionAggregate" for node in plan.semantic_dag.nodes):
        notes.append(
            "distribution branch is compiled through branch lowering; inspect rendered SQL for branch-local scan details"
        )

    return PerformancePlan(
        estimated_scan_relations=estimated_scan_relations,
        raw_fact_count=raw_fact_count,
        date_filter_pushdown=date_filter_pushdown,
        pre_aggregation_grain=pre_aggregation_grain,
        joins_before_aggregate=joins_before_aggregate,
        joins_after_aggregate=joins_after_aggregate,
        full_outer_alignments=full_outer_alignments,
        large_scan_risk={
            "risk": risk_level != "low",
            "reasons": risk_reasons,
            "large_raw_relations": large_raw_relations,
            "time_window_days": window_days,
            "all_raw_scans_bounded": all_raw_bounded,
            "source_preaggregates": source_preaggregates,
        },
        risk_level=risk_level,
        execution_recommendation=execution_recommendation,
        semantic_equivalence="exact_primitives",
        notes=notes,
        aggregate_routing=aggregate_routing_report(plan, physical_plan, config, rollup_scans),
    )
