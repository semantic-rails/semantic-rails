"""The aggregate-routing kill switch and the per-leaf routing report.

A runtime turns routing to declared rollups on or off (``SEMANTIC_RAILS_AGGREGATE_ROUTING``
or ``Runtime.set_aggregate_routing``). Its request scope enters :func:`aggregate_routing`, the
planner rejects every rollup with :data:`ROUTING_OFF` while it is off, and the compile cache
keys on :func:`aggregate_routing_enabled`, so a switch applies to the next request even when
the plan is cached.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from ..errors import SemanticLayerError

if TYPE_CHECKING:
    from ..config import PackageConfig
    from ..ir import LogicalPlan, PhysicalPlan

AGGREGATE_ROUTING_ENV = "SEMANTIC_RAILS_AGGREGATE_ROUTING"
ROUTING_OFF = "aggregate_routing_off"
NOT_LOWERED = "query_shape_not_routed"  # e.g. distribution and entity-set plans scan base tables
_enabled: ContextVar[bool] = ContextVar("semantic_rails_aggregate_routing", default=True)


def parse_aggregate_routing(value: str) -> bool:
    """Read an ``on``/``off`` setting; unset means on."""
    text = str(value or "on").strip().lower()
    if text not in {"on", "off"}:
        raise SemanticLayerError(
            "INVALID_CONFIG", f"{AGGREGATE_ROUTING_ENV} must be 'on' or 'off', not {value!r}"
        )
    return text == "on"


@contextmanager
def aggregate_routing(enabled: bool) -> Iterator[None]:
    """Route to rollups inside this block only if ``enabled``; a nested block can't turn it on."""
    token = _enabled.set(_enabled.get() and bool(enabled))
    try:
        yield
    finally:
        _enabled.reset(token)


def aggregate_routing_enabled() -> bool:
    return _enabled.get()


def routing_candidates(
    plan: LogicalPlan, physical: PhysicalPlan, config: PackageConfig
) -> list[dict[str, str]]:
    """Each rollup considered for each measure leaf: ``selected``, ``eligible`` or ``rejected``.

    A leaf's pick counts as ``selected`` only if the physical plan scans that rollup for it;
    otherwise the leaf ran on the base tables and its rollups are rejected with :data:`NOT_LOWERED`.
    """
    scanned = {
        (str(item.get("alias", "")), str(node.details.get("aggregate_relation_id", "")))
        for node in physical.nodes
        if node.kind == "Scan"
        and node.details.get("selected_relation_type") == "aggregate_relation"
        for item in node.details.get("measures", []) or []
    }
    rows: list[dict[str, str]] = []
    for leaf in plan.measure_plans:
        chosen = leaf.aggregate_relation_id
        lowered = (leaf.bound_measure.alias, chosen) in scanned
        for relation in config.aggregate_relations:
            if relation.source_entity != leaf.source_entity:
                continue
            reason = leaf.aggregate_relation_rejections.get(relation.id, "")
            if not reason and chosen and not lowered:
                reason = NOT_LOWERED
            picked = "selected" if relation.id == chosen else "eligible"
            decision = "rejected" if reason else picked
            rows.append(
                {
                    "leaf_id": leaf.cte_name,
                    "measure_id": leaf.bound_measure.measure_id,
                    "relation_id": relation.id,
                    "decision": decision,
                    "reason": reason,
                }
            )
    return rows
