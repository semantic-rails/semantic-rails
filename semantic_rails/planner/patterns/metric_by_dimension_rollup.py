"""Pattern: ``metric_by_dimension_rollup`` catch-all.

Detects the simple "measure / metric (by dimension(s)) (top N)"
shape that doesn't fit any of the more specialized patterns
(qualified rollup, period shift, adoption funnel, distribution,
ratio, arithmetic). Examples:

* "top stores by revenue"
* "monthly revenue by store"
* "revenue by customer segment"
* "orders trending over time"

This pattern returns ``score=0.5`` so it only wins when no more
specific pattern (which returns the default ``score=1.0``) claims
the intent.
"""

from __future__ import annotations

import re
from typing import Any

from .._base import (
    _TERM_SYNONYMS,
    RuntimeCompositionDraft,
    _add_order,
    _aggregation_from_text,
    _explicit_grain,
    _implied_window_grain,
    _maybe_group_by,
    _object_by_id,
    _preferred_measure,
    _preferred_metric,
    _resolved,
    _semantic_token,
    _target_measure_terms,
    _time_bounds_from_text,
    _time_spec,
    _tokens,
    _top_n_intent,
    _unresolved_time_phrases,
)
from ..generators import _matched_value_rows, _target_focus_text
from ._protocol import IntentPattern

# Catch-all patterns return a sub-unity score so the orchestrator
# prefers a specific pattern when both fire. Picked low enough that
# any plausible competitor wins, high enough to leave headroom if
# future catch-alls want to rank against each other.
_CATCH_ALL_SCORE = 0.5

_TIME_SERIES_PHRASES = (
    "over time",
    "historical",
    "history",
    "trend",
    "trending",
    "end-of-month",
    "end of month",
)


def _match(runtime: Any, text: str, terms: set[str]) -> RuntimeCompositionDraft | None:
    config = runtime.config
    lowered = str(text or "").lower()
    target_focus = _target_focus_text(text)
    target_focus_terms = set(_tokens(target_focus))
    target_terms = (
        _target_measure_terms(target_focus, target_focus_terms)
        or _target_measure_terms(text, terms)
        or sorted(target_focus_terms)
    )
    group_by = _maybe_group_by(config, text, target_terms=target_terms)
    metric_first = bool(
        (set(target_terms) | target_focus_terms)
        & {
            "adoption",
            "average",
            "conversion",
            "funnel",
            "growth",
            "high",
            "rate",
            "repeat",
            "ratio",
            "share",
            "utilization",
        }
    )
    if not metric_first and "per" in target_focus_terms:
        # "per" cues a ratio metric ("revenue per order") unless it names a
        # grouping this intent already resolved — "revenue per store" is just
        # "revenue by store" and must keep the plain measure.
        per_match = re.search(r"\bper\s+([a-z0-9]+)", target_focus)
        per_word = _TERM_SYNONYMS.get(per_match.group(1), per_match.group(1)) if per_match else ""
        grouped_tokens: set[str] = set()
        for dim_id in group_by:
            grouped_tokens.update(_tokens(dim_id))
            dim = _object_by_id(config.dimensions, dim_id)
            if dim is not None:
                grouped_tokens.update(_tokens(getattr(dim, "label", "")))
        metric_first = not per_word or per_word not in grouped_tokens

    # Resolve a target — measure preferred over metric (measures carry
    # the cleanest scope semantics), but we accept either. We try the
    # target_terms hint first; if the intent doesn't carry one of the
    # canonical concept words (revenue / order / arr / etc.) we just
    # use the raw term set against the catalog.
    target = None
    if target_terms:
        if metric_first:
            target = _preferred_metric(config, target_terms)
            if target is None:
                target = _preferred_measure(config, target_terms)
        else:
            target = _preferred_measure(config, target_terms)
            if target is None:
                target = _preferred_metric(config, target_terms)
    if target is None:
        if metric_first:
            target = _preferred_metric(config, terms)
            if target is None:
                target = _preferred_measure(config, terms)
        else:
            target = _preferred_measure(config, terms)
            if target is None:
                target = _preferred_metric(config, terms)
    if target is None:
        return None

    # Build the time spec from the target's default temporal role (if
    # any). Without a temporal role we still emit a query without a
    # ``time`` block — the validator handles that for snapshot-style
    # measures.
    is_top, top_n = _top_n_intent(text)
    time_spec: dict[str, Any] | None = None
    temporal_role = str(getattr(target, "default_temporal_role", "") or "") or str(
        getattr(target, "temporal_role", "") or ""
    )
    if temporal_role:
        time_spec = _time_spec(temporal_role, text)
        if (
            not _explicit_grain(text)
            and not _implied_window_grain(lowered)
            and not _time_bounds_from_text(text)
            and not any(phrase in lowered for phrase in _TIME_SERIES_PHRASES)
            and not _unresolved_time_phrases(text)
        ):
            # A default temporal role is a valid execution anchor, not
            # evidence that the user requested a series. "total orders",
            # "revenue", and "AOV" are all-time scalars; "orders by store"
            # is one row per store. A synthetic month bucket changes every
            # one of those semantic shapes, so time only stays when the
            # intent carries a real time cue.
            time_spec = None

    target_id = str(getattr(target, "id", ""))
    is_measure = target_id.startswith("measure.")
    select_alias = _semantic_token(target_id, fallback="value")
    if is_measure:
        expression: dict[str, Any] = {
            "measure": target_id,
            "aggregation": _aggregation_from_text(text, terms, target),
        }
    else:
        expression = {"metric": target_id}

    query: dict[str, Any] = {
        "version": 2,
        "select": [{"as": select_alias, "expression": expression}],
    }
    if time_spec is not None:
        query["time"] = time_spec

    if group_by:
        query["group_by"] = group_by
    where = [
        {"field": row["dimension_id"], "op": "=", "value": row["value"]}
        for row in _matched_value_rows(runtime, query, text)
    ]
    if where:
        query["where"] = where

    if is_top:
        query["order_by"] = [{"field": select_alias, "direction": "DESC"}]
        query["limit"] = top_n
    else:
        _add_order(query)

    resolved: list[dict[str, Any]] = [_resolved(target)]
    for dim_id in group_by:
        dim = _object_by_id(config.dimensions, dim_id)
        if dim is not None:
            resolved.append(_resolved(dim))

    return RuntimeCompositionDraft(
        query=query,
        resolved=resolved,
        rationale=[
            "composed a metric_by_dimension_rollup from the intent's target measure or metric and any group-by dimensions",
        ],
        interpreted_intent={
            "pattern": "metric_by_dimension_rollup",
            "source": "plan",
            "target": target_id,
            "target_kind": "measure" if is_measure else "metric",
            "group_by": list(group_by),
            "is_top_intent": is_top,
            "top_n": top_n if is_top else None,
        },
        score=_CATCH_ALL_SCORE,
    )


PATTERN = IntentPattern(
    name="metric_by_dimension_rollup",
    match=_match,
)
