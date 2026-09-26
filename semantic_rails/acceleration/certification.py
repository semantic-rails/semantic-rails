"""The engine's side of certifying a declared rollup: its verdict and a paired query per column.

A host that materializes rollups (and marks them ``requires_certification``) calls
:func:`certify_aggregate_relation`, runs each measure's ``base_sql`` and ``rollup_sql`` on the
warehouse, and certifies the rollup only if every measure routes and every row matches. Its
:class:`~.routing.CertificationProvider` then says yes for that rollup.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..compiler import compile_query
from ..compiler_parts.indexes import _measure_index, _temporal_role_index
from ..errors import SemanticLayerError
from ..schema import PackageConfig
from .routing import LOWERED_SEPARATELY, aggregate_routing
from .selection import _aggregate_dimension_coverage, _column_holds, _grain_rank


def certify_aggregate_relation(config: PackageConfig, relation_id: str) -> dict[str, Any]:
    """Judge one declared rollup, as if certified, against the routing rules (R1-R8).

    Each measure column gets one query: what the column holds, grouped by every rollup
    dimension, over all time, at the finest grain the rollup may answer (the finest of its
    eligible grains that its time role supports). ``reason`` is the rule that query fails, or
    ``""`` when the rollup answers it; ``rollup_sql`` reads the rollup and ``base_sql`` the base
    tables. Every other query the rules let the rollup answer re-aggregates these rows: coarser
    grains, fewer of its own dimensions, filters; a pre-joined column routes only for queries
    that use it. The caller's routing switch applies, so with routing off every measure fails
    with ``aggregate_routing_off``.
    """
    relation = next((row for row in config.aggregate_relations if row.id == relation_id), None)
    if relation is None:
        raise SemanticLayerError("OBJECT_NOT_FOUND", f"Unknown aggregate relation '{relation_id}'")
    alone = replace(config, aggregate_relations=[replace(relation, requires_certification=False)])
    measures = _measure_index(config)
    role = _temporal_role_index(config).get(relation.temporal_role)
    supported = set(getattr(role, "supported_grains", []) or [])
    grains = [
        grain for grain in relation.eligible_time_grains or [relation.grain] if grain in supported
    ]
    results: list[dict[str, Any]] = []
    for measure_id in relation.measure_columns:
        if role is None or not grains:  # routing needs a declared role and a grain it can ask
            reason = "temporal_role_mismatch" if role is None else "unsupported_query_grain"
            results.append(
                {"measure_id": measure_id, "query": None, "base_sql": "", "reason": reason}
            )
            continue
        time = {"temporal_role": relation.temporal_role, "grain": min(grains, key=_grain_rank)}
        holds, _ = _column_holds(relation, measures[measure_id])
        aggregation = holds or measures[measure_id].default_aggregation
        expression = {"kind": "aggregate", "measure": measure_id, "aggregation": aggregation}
        query = {
            "version": 1,
            "select": [{"expression": expression, "as": "value"}],
            "group_by": sorted(_aggregate_dimension_coverage(relation)),
            "time": time,
        }
        result: dict[str, Any] = {"measure_id": measure_id, "query": query, "base_sql": ""}
        try:
            routed = compile_query(alone, None, query)
            with aggregate_routing(False):
                result["base_sql"] = compile_query(config, None, query)["sql"]
        except SemanticLayerError as exc:
            results.append({**result, "reason": "query_not_compiled", "error": exc.code})
            continue
        leaf = routed["logical_plan"].measure_plans[0]
        reason = leaf.aggregate_relation_rejections.get(relation.id, "")
        if (
            not reason
            and relation.id not in routed["performance_plan"].aggregate_routing["selected"]
        ):
            reason = LOWERED_SEPARATELY
        results.append({**result, "reason": reason, "rollup_sql": "" if reason else routed["sql"]})
    return {
        "relation_id": relation.id,
        "certifiable": bool(results) and not any(item["reason"] for item in results),
        "measures": results,
    }
