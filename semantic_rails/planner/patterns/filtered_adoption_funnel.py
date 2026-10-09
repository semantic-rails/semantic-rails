"""Pattern: ``filtered_adoption_funnel``.

Detects adoption-funnel intents (signup → send conversion) and optionally qualifies the
entities by an order-rate threshold.
"""

from __future__ import annotations

from typing import Any

from .._base import RuntimeCompositionDraft, _dimension, _metric, _object_by_id, _resolved
from ..call import QueryBuilder
from ..groupings import _maybe_group_by, _time_spec
from ..qualifiers import _add_order, _qualifying_entity, _threshold_from_text
from ._protocol import IntentPattern
from .qualified_metric_rollup import _over, _period_options, _qualified


def _match(runtime: Any, text: str, terms: set[str]) -> RuntimeCompositionDraft | None:
    wants_adoption = bool({"adoption", "funnel"} & terms) or (
        "signup" in terms and "received" in terms
    )
    if not wants_adoption:
        return None
    config = runtime._config
    metric = _metric(config, ["signup", "send", "adoption", "funnel"])
    if metric is None:
        return None
    group_by = _maybe_group_by(config, text)
    entity = _qualifying_entity(config, terms, text=text)
    if not group_by and entity is not None and "store" in str(entity.id):
        dim = _dimension(config, ["store", "name"])
        group_by = [dim.id] if dim is not None else []

    def builder_for() -> QueryBuilder:
        builder = QueryBuilder(config).select(metric.id)
        if getattr(metric, "temporal_role", ""):
            builder.time(_time_spec(metric.temporal_role, text))
        return _add_order(builder.groups(group_by))

    threshold = _threshold_from_text(text)
    predicate_metric = None
    if (
        threshold is not None
        and entity is not None
        and "order" in terms
        and ("rate" in terms or "pct" in terms)
    ):
        predicate_metric = _metric(config, ["session", "order", "conversion", "rate"])
    query = builder_for().build()
    why: dict[str, Any] = {}
    if predicate_metric is not None and threshold is not None and entity is not None:
        op, value = threshold
        over = _over(text, query)
        query = _qualified(
            config, builder_for, over or "all_time", entity, [predicate_metric], op, value
        )
        if not over:
            why = {
                "code": "ENTITY_FILTER_PERIOD_UNSTATED",
                "message": (
                    f"The question qualifies each {entity.label or entity.id} by its order "
                    "rate but doesn't say over which rows it is taken."
                ),
                "details": {
                    "clarification": {"question": "Over which rows is the rate taken?"},
                    "options": _period_options(
                        config, builder_for, entity, [predicate_metric], op, value
                    ),
                },
                "recovery_hints": [
                    {"kind": "choose_over", "message": "Send the call of the reading you mean."}
                ],
            }
    resolved = [_resolved(metric)]
    if predicate_metric is not None:
        resolved.append(_resolved(predicate_metric))
    if entity is not None:
        resolved.append(_resolved(entity))
    for dim_id in group_by:
        dim = _object_by_id(config.dimensions, dim_id)
        if dim is not None:
            resolved.append(_resolved(dim))

    return RuntimeCompositionDraft(
        query=query,
        resolved=resolved,
        rationale=["composed adoption funnel metric with an optional order-rate qualification"],
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
        ask=why,
    )


PATTERN = IntentPattern(name="filtered_adoption_funnel", match=_match)
