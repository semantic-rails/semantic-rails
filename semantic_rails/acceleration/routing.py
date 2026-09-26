"""The aggregate-routing kill switch and the per-leaf routing report.

A runtime turns routing to declared rollups on or off (``SEMANTIC_RAILS_AGGREGATE_ROUTING``
or ``Runtime.set_aggregate_routing``). Its request scope enters :func:`aggregate_routing`, the
planner rejects every rollup with :data:`ROUTING_OFF` while it is off, and the compile cache
keys on :func:`aggregate_routing_enabled`, so a switch applies to the next request even when
the plan is cached.
"""

from __future__ import annotations

from collections import defaultdict
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
LOWERED_SEPARATELY = "lowered_separately"  # a distribution branch or an entity-set plan
MAX_CANDIDATES = 200  # report rows; the rest are counted in `candidates_omitted`
_enabled: ContextVar[bool] = ContextVar("semantic_rails_aggregate_routing", default=True)
_scans: ContextVar[set[str] | None] = ContextVar("semantic_rails_rollup_scans", default=None)


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


@contextmanager
def recording_rollup_scans() -> Iterator[set[str]]:
    """Collect every rollup the SQL lowered inside this block reads, nested compiles included."""
    scans: set[str] = set()
    token = _scans.set(scans)
    try:
        yield scans
    finally:
        _scans.reset(token)


def record_rollup_scan(relation_id: str) -> None:
    scans = _scans.get()
    if scans is not None:
        scans.add(relation_id)


def aggregate_routing_report(
    plan: LogicalPlan, physical: PhysicalPlan, config: PackageConfig, scans: frozenset[str]
) -> dict[str, object]:
    """``selected``: every rollup the compiled SQL reads (``scans``); ``candidates``: each rollup
    considered for each measure leaf, ``selected``, ``eligible``, ``rejected`` or ``unknown``.

    A leaf's pick counts as ``selected`` only if the physical plan scans that rollup for it. A
    leaf lowered some other way (a distribution compiles each branch as its own query) reports,
    with reason :data:`LOWERED_SEPARATELY`, each rollup its planner didn't reject as ``unknown``
    when some branch reads it, and as ``eligible`` when none does.
    """
    scanned = {
        (str(item.get("alias", "")), str(node.details.get("aggregate_relation_id", "")))
        for node in physical.nodes
        if node.kind == "Scan"
        and node.details.get("selected_relation_type") == "aggregate_relation"
        for item in node.details.get("measures", []) or []
    }
    by_entity = defaultdict(list)
    for relation in config.aggregate_relations:
        by_entity[relation.source_entity].append(relation)
    rows: list[dict[str, str]] = []
    for leaf in plan.measure_plans:
        chosen = leaf.aggregate_relation_id
        unverified = chosen and (leaf.bound_measure.alias, chosen) not in scanned
        for relation in by_entity[leaf.source_entity]:
            reason = leaf.aggregate_relation_rejections.get(relation.id, "")
            if reason:
                decision = "rejected"
            elif unverified:
                reason = LOWERED_SEPARATELY
                decision = "unknown" if relation.id in scans else "eligible"
            else:
                decision = "selected" if relation.id == chosen else "eligible"
            rows.append(
                {
                    "leaf_id": leaf.cte_name,
                    "measure_id": leaf.bound_measure.measure_id,
                    "relation_id": relation.id,
                    "decision": decision,
                    "reason": reason,
                }
            )
    report: dict[str, object] = {
        "selected": sorted(scans),
        "selected_count": len(scans),
        "candidates": rows[:MAX_CANDIDATES],
    }
    if len(rows) > MAX_CANDIDATES:
        report["candidates_omitted"] = len(rows) - MAX_CANDIDATES
    return report
