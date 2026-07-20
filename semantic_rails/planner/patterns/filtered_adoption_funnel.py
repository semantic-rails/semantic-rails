"""Pattern: ``filtered_adoption_funnel``.

Detects adoption-funnel intents (signup → send conversion) and
optionally attaches a contextual order-rate filter.
"""

from __future__ import annotations

from typing import Any

from .._base import (
    RuntimeCompositionDraft,
    _add_order,
    _dimension,
    _maybe_group_by,
    _metric,
    _object_by_id,
    _qualifying_entity,
    _resolved,
    _threshold_from_text,
    _time_spec,
)
from ._protocol import IntentPattern


def _match(runtime: Any, text: str, terms: set[str]) -> RuntimeCompositionDraft | None:
    wants_adoption = bool({"adoption", "funnel"} & terms) or (
        "signup" in terms and "received" in terms
    )
    if not wants_adoption:
        return None
    metric = _object_by_id(
        runtime.config.metric_recipes, "metric.adoption.signup_to_send_conversion_rate_28d"
    )
    if metric is None:
        metric = _metric(runtime.config, ["signup", "send", "adoption", "funnel"])
    if metric is None:
        return None

    query: dict[str, Any] = {
        # All other patterns emit v2 IR; v1 here was a leftover that
        # eroded agent trust ("why does this one tool use a different
        # version?"). Snapshot tests pin behavior, not the version
        # number, so bumping is safe.
        "version": 2,
        "select": [{"as": "signup_to_send_28d", "expression": {"metric": metric.id}}],
    }
    if getattr(metric, "temporal_role", ""):
        query["time"] = _time_spec(metric.temporal_role, text)

    group_by = _maybe_group_by(runtime.config, text)
    entity = _qualifying_entity(runtime.config, terms, text=text)
    if not group_by and entity is not None and entity.id == "entity.jaffle_store":
        dim = _dimension(runtime.config, ["store", "name"])
        group_by = [dim.id] if dim is not None else []
    if group_by:
        query["group_by"] = group_by

    threshold = _threshold_from_text(text)
    predicate_metric = None
    if (
        threshold is not None
        and entity is not None
        and "order" in terms
        and ("rate" in terms or "pct" in terms)
    ):
        predicate_metric = _object_by_id(
            runtime.config.metric_recipes,
            "metric.sales.session_to_order_conversion_rate_7d_same_store",
        )
        if predicate_metric is None:
            predicate_metric = _metric(runtime.config, ["session", "order", "conversion", "rate"])
        if predicate_metric is not None:
            op, value = threshold
            query["metric_filters"] = [
                {
                    "expression": {
                        "kind": "metric_predicate",
                        "entity": entity.id,
                        "scope_mode": "contextual",
                        "input": {"metric": predicate_metric.id},
                        "op": op,
                        "value": value,
                    },
                    "op": "=",
                    "value": True,
                }
            ]

    _add_order(query)
    resolved = [_resolved(metric)]
    if predicate_metric is not None:
        resolved.append(_resolved(predicate_metric))
    if entity is not None:
        resolved.append(_resolved(entity))
    for dim_id in group_by:
        dim = _object_by_id(runtime.config.dimensions, dim_id)
        if dim is not None:
            resolved.append(_resolved(dim))

    return RuntimeCompositionDraft(
        query=query,
        resolved=resolved,
        rationale=["composed adoption funnel metric with optional contextual order-rate filter"],
        interpreted_intent={
            "pattern": "filtered_adoption_funnel",
            "source": "plan",
            "metric": metric.id,
            "qualifying_entity": getattr(entity, "id", "") if entity is not None else "",
            "predicate_metric": getattr(predicate_metric, "id", "")
            if predicate_metric is not None
            else "",
            "threshold": threshold,
        },
    )


PATTERN = IntentPattern(name="filtered_adoption_funnel", match=_match)
