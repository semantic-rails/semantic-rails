"""Pattern: ``qualified_metric_rollup``.

Detects intents that ask for a metric rolled up to one entity grain
while filtering by a count/threshold predicate evaluated at another
("top stores by revenue with at least 4 distinct customers").
"""

from __future__ import annotations

from typing import Any

from .._base import (
    _DEFAULT_TOP_LIMIT,
    RuntimeCompositionDraft,
    _add_order,
    _aggregation_from_text,
    _maybe_group_by,
    _object_by_id,
    _predicate_grain,
    _predicate_input,
    _predicate_metric_terms,
    _preferred_measure,
    _preferred_predicate_target,
    _product_filter,
    _qualification_phrase,
    _qualification_phrase_token_groups,
    _qualifying_entity,
    _resolved,
    _semantic_token,
    _target_measure_terms,
    _threshold_from_text,
    _time_spec,
    _top_n_intent,
)
from ._protocol import IntentPattern


def _match(runtime: Any, text: str, terms: set[str]) -> RuntimeCompositionDraft | None:
    threshold = _threshold_from_text(text)
    if threshold is None:
        return None
    entity = _qualifying_entity(runtime.config, terms, text=text)
    if entity is None:
        return None

    target_terms = _target_measure_terms(text, terms)
    if not target_terms:
        return None
    target_measure = _preferred_measure(runtime.config, target_terms)
    if target_measure is None or not target_measure.default_temporal_role:
        return None

    predicate_targets: list[Any] = []
    predicate_term_groups = _predicate_metric_terms(text, terms, target_terms)
    for metric_terms in predicate_term_groups:
        target = _preferred_predicate_target(
            runtime.config, metric_terms, target_measure=target_measure
        )
        if target is not None and getattr(target, "id", "") not in {
            getattr(row, "id", "") for row in predicate_targets
        }:
            predicate_targets.append(target)

    op, value = threshold
    time_spec = _time_spec(target_measure.default_temporal_role, text)
    output_grain = str(time_spec.get("grain", "") or "")
    explicit_predicate_grain = _predicate_grain(text, output_grain)
    is_top, top_n = _top_n_intent(text)

    if not predicate_targets:
        # Pattern matched but no predicate resolved — surface as a
        # structured blocked draft so the orchestrator can return
        # recovery hints to the agent.
        phrase_groups = _qualification_phrase_token_groups(text, target_terms)
        unresolved_phrase = _qualification_phrase(text)
        select_alias = (
            f"{_semantic_token(target_measure.id)}_from_qualified_{_semantic_token(entity.id)}s"
        )
        placeholder_query: dict[str, Any] = {
            "version": 2,
            "select": [
                {
                    "as": select_alias,
                    "expression": {
                        "measure": target_measure.id,
                        "aggregation": _aggregation_from_text(text, terms, target_measure),
                    },
                }
            ],
            "time": time_spec,
        }
        group_by = _maybe_group_by(runtime.config, text, target_terms=target_terms)
        if group_by:
            placeholder_query["group_by"] = group_by
        if is_top:
            placeholder_query["order_by"] = [{"field": select_alias, "direction": "DESC"}]
            placeholder_query["limit"] = top_n
        else:
            _add_order(placeholder_query)
        return RuntimeCompositionDraft(
            query=placeholder_query,
            resolved=[_resolved(target_measure), _resolved(entity)],
            rationale=[
                "matched qualified_metric_rollup pattern but could not resolve a predicate metric/measure for the qualification phrase",
            ],
            interpreted_intent={
                "pattern": "qualified_metric_rollup",
                "source": "plan",
                "target_measure": target_measure.id,
                "predicate_metrics": [],
                "qualifying_entity": entity.id,
                "output_grain": output_grain,
                "predicate_grain": explicit_predicate_grain or output_grain,
                "predicate_grain_explicit": bool(explicit_predicate_grain),
            },
            blocked_reason={
                "code": "QUALIFICATION_NOT_REALIZABLE",
                "message": (
                    "Detected a qualified metric rollup but could not resolve a "
                    "predicate metric or measure from the qualification phrase."
                ),
                "details": {
                    "qualification_phrase": unresolved_phrase,
                    "qualification_tokens": [token for group in phrase_groups for token in group],
                    "target_measure": target_measure.id,
                    "qualifying_entity": entity.id,
                },
                "recovery_hints": [
                    "Rephrase the qualification so the predicate names a measure or metric — e.g. 'with at least 4 orders' or 'who placed more than 10 orders'.",
                    "Inspect available measures via /api/v1/discover?terms=<predicate keyword>.",
                ],
            },
        )

    predicates: list[dict[str, Any]] = []
    metric_filters: list[dict[str, Any]] = []
    for target in predicate_targets:
        input_ref = _predicate_input(target)
        predicate: dict[str, Any] = {
            **input_ref,
            "entity": entity.id,
            "scope_mode": "contextual",
            "op": op,
            "value": value,
        }
        if explicit_predicate_grain:
            predicate["time_grain"] = explicit_predicate_grain
        predicates.append(predicate)
        metric_filter_expr: dict[str, Any] = {
            "kind": "metric_predicate",
            "entity": entity.id,
            "scope_mode": "contextual",
            "input": input_ref,
            "op": op,
            "value": value,
        }
        if explicit_predicate_grain:
            metric_filter_expr["time_grain"] = explicit_predicate_grain
        metric_filters.append({"expression": metric_filter_expr, "op": "=", "value": True})

    expression: dict[str, Any] = {
        "kind": "scoped_aggregate",
        "measure": target_measure.id,
        "aggregation": _aggregation_from_text(text, terms, target_measure),
        "predicates": predicates,
    }
    product_filter = _product_filter(runtime.config, "account") if "arr" in target_terms else None
    if product_filter is not None:
        expression["where"] = [product_filter]

    select_alias = (
        f"{_semantic_token(target_measure.id)}_from_qualified_{_semantic_token(entity.id)}s"
    )
    query: dict[str, Any] = {
        "version": 2,
        "select": [
            {
                "as": select_alias,
                "expression": expression,
            }
        ],
        "time": time_spec,
        "metric_filters": metric_filters,
    }
    group_by = _maybe_group_by(runtime.config, text, target_terms=target_terms)
    if group_by:
        query["group_by"] = group_by
    if is_top:
        query["order_by"] = [{"field": select_alias, "direction": "DESC"}]
        query["limit"] = top_n
    else:
        _add_order(query)
        query["limit"] = _DEFAULT_TOP_LIMIT

    resolved = [
        _resolved(target_measure),
        *[_resolved(target) for target in predicate_targets],
        _resolved(entity),
    ]
    if group_by:
        dim = _object_by_id(runtime.config.dimensions, group_by[0])
        if dim is not None:
            resolved.append(_resolved(dim))
    return RuntimeCompositionDraft(
        query=query,
        resolved=resolved,
        rationale=[
            "composed qualified metric rollup from target measure, predicate metric/measure, qualifying entity, and query-time grain",
            "synthesized metric_filters + limit at the top level so the IR explicitly names the qualification cohort",
        ],
        interpreted_intent={
            "pattern": "qualified_metric_rollup",
            "source": "plan",
            "target_measure": target_measure.id,
            "predicate_metrics": [getattr(target, "id", "") for target in predicate_targets],
            "qualifying_entity": entity.id,
            "output_grain": str(time_spec.get("grain", "") or ""),
            "predicate_grain": explicit_predicate_grain or str(time_spec.get("grain", "") or ""),
            "predicate_grain_explicit": bool(explicit_predicate_grain),
            "limit": query["limit"],
            "is_top_intent": is_top,
        },
    )


PATTERN = IntentPattern(name="qualified_metric_rollup", match=_match)
