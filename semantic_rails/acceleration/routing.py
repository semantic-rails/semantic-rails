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
    from ..ir import LogicalPlan

AGGREGATE_ROUTING_ENV = "SEMANTIC_RAILS_AGGREGATE_ROUTING"
ROUTING_OFF = "aggregate_routing_off"
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


def routing_candidates(plan: LogicalPlan, config: PackageConfig) -> list[dict[str, str]]:
    """Each rollup considered for each measure leaf: ``selected``, ``eligible`` or ``rejected``."""
    rows: list[dict[str, str]] = []
    for leaf in plan.measure_plans:
        for relation in config.aggregate_relations:
            if relation.source_entity != leaf.source_entity:
                continue
            reason = leaf.aggregate_relation_rejections.get(relation.id, "")
            if relation.id == leaf.aggregate_relation_id:
                decision = "selected"
            else:
                decision = "rejected" if reason else "eligible"
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
