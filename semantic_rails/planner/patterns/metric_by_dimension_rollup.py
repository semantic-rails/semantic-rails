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

from ...config_parts.measure_governance import (
    building_block_measures,
    governing_metrics,
    whole_aggregate,
)
from ...errors import SemanticLayerError
from ...expressions import collect_object_references
from ...naming import semantic_token as _semantic_token
from .._base import (
    _NAME_CONNECTORS,
    _TERM_SYNONYMS,
    RuntimeCompositionDraft,
    _aggregation_from_text,
    _named_metric,
    _object_by_id,
    _preferred_measure,
    _preferred_metric,
    _resolved,
    _said_name,
    _tokens,
)
from ..generators import _matched_value_rows, _normalize_value_filters, _target_focus_text
from ..groupings import _explicit_grain, _maybe_group_by, _time_spec
from ..intent_ir import _FALLBACK_STOPWORDS
from ..qualifiers import _add_order, _target_measure_terms, _top_n_intent
from ..time_windows import _time_bounds_from_text, _time_window
from ..visibility import visible_object_ids
from ._protocol import IntentPattern


def _governed_target(config: Any, focus: str, query: dict[str, Any]) -> Any | None:
    """The metric a one-select draft over a measure answers with instead.

    The select reads a measure, or the metric that is its plain aggregate. A metric that
    aggregates that measure the same way through a filter governs it ("Active stores" over
    "Active stores (all kinds)"). It is the answer when the question's target phrase ``focus``
    names it (``_said_name``), and names no other such metric as fully nor the measure more
    fully; or when the measure is a building block and this metric alone governs it. Never
    when the draft filters or groups by something its filter reads: "demo stores" asks for
    rows the governed metric leaves out.
    """

    select = list(query.get("select") or [])
    expression = select[0].get("expression") if len(select) == 1 else None
    if not isinstance(expression, dict):
        return None
    plain = _object_by_id(config.metric_recipes, str(expression.get("metric", "")))
    whole = whole_aggregate(plain) if plain is not None else None
    if plain is not None and (whole is None or whole[2]):
        return None
    measure_id, aggregation = whole[:2] if whole else (expression.get("measure"), "")
    measure = _object_by_id(config.measures, str(measure_id or ""))
    if measure is None:
        return None
    aggregation = aggregation or expression.get("aggregation") or measure.default_aggregation
    governing = governing_metrics(config, measure.id)
    visible = set(visible_object_ids(config, (metric.id for metric in governing)))
    governing = [metric for metric in governing if metric.id in visible]
    candidates = {
        metric.id: (metric, governed[2])
        for metric in governing
        if (governed := whole_aggregate(metric)) is not None
        and governed[0] == measure.id
        and (governed[1] or measure.default_aggregation) == aggregation
    }
    named = {metric.id: words for metric in governing if (words := _said_name(metric, focus))}
    widest = [key for key in named if all(words <= named[key] for words in named.values())]
    if named:
        chosen = widest[0] if len(widest) == 1 else ""
    elif measure.id in building_block_measures(config) and len(governing) == 1:
        chosen = governing[0].id
    else:
        chosen = ""
    asked = _said_name(measure, focus) | (_said_name(plain, focus) if plain else frozenset())
    if chosen not in candidates or not asked <= named.get(chosen, frozenset()):
        return None
    metric, narrowing = candidates[chosen]
    try:
        cuts = {key: query.get(key) for key in ("where", "group_by", "metric_filters")}
        if set(collect_object_references(narrowing, config)) & set(
            collect_object_references(cuts, config)
        ):
            return None
    except SemanticLayerError:
        return None
    return metric


def _named_measure(config: Any, text: str, ordinary: Any | None = None) -> Any | None:
    """The measure the question names by a label or alias of two words or more.

    An exact multi-word name outranks a partial one: "item revenue" names Item revenue,
    not Revenue, which only shares a word with it. The longest name wins; a tie between
    different measures names none, so the ordinary ranking decides. The question's words
    stay in order, so "revenue by item" names neither.

    The name replaces the ordinary reading (``ordinary``, the target the ranking chose)
    only when it contains every word of that target's label: "large order revenue" names
    Large orders inside it, but asks for revenue, so the ordinary target stays. Another
    measure noun right after the name ("item revenue orders") leaves the question alone too.
    """

    said = _tokens(text)
    best_size = 0
    best: list[tuple[Any, tuple[str, ...], int]] = []
    for row in config.measures:
        names = [row.label, re.sub(r"\s*\(.*?\)", "", str(row.label or "")), *(row.aliases or [])]
        for name in names:
            parts = _tokens(name)
            size = len(parts)
            if size < 2 or size < best_size:
                continue
            at = next(
                (
                    start
                    for start in range(len(said) - size + 1)
                    if said[start : start + size] == parts
                ),
                None,
            )
            if at is None:
                continue
            if size > best_size:
                best_size, best = size, []
            if all(item[0] is not row for item in best):
                best.append((row, parts, at + size))
    if len(best) != 1:
        return None
    row, parts, end = best[0]
    if ordinary is not None:
        label = re.sub(r"\s*\(.*?\)", "", str(getattr(ordinary, "label", "") or ""))
        if not set(_tokens(label)) <= set(parts):
            return None
    following = said[end] if end < len(said) else ""
    if following and following not in _NAME_CONNECTORS:
        other_nouns = {
            token
            for other in config.measures
            if other is not row
            for token in _tokens(getattr(other, "label", ""))
        }
        if following in other_nouns:
            return None
    return row


def _implied_window_grain(lowered: str) -> str:
    """Grain implied by a resolved relative window, or ``""``.

    "last 7 days" implies a daily series; "yesterday"/"today" imply a
    single day bucket.
    """

    return _time_window(lowered).relative_unit


def _unresolved_time_phrases(text: str) -> list[str]:
    """Time phrases the planner detected but did not resolve into a window.

    ``plan`` reports them (``TIME_WINDOW_UNRESOLVED``) instead of marking a
    draft ready, because the draft doesn't carry the window the question
    asked for.
    """

    return list(_time_window(text).unresolved)


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
    config = runtime._config
    named = _named_metric(config, text)
    if named is not None:
        text = named[1]
        terms = set(_tokens(text))
    lowered = str(text or "").lower()
    target_focus = _target_focus_text(text)
    target_focus_terms = set(_tokens(target_focus))
    target_terms = (
        _target_measure_terms(target_focus, target_focus_terms)
        or _target_measure_terms(text, terms)
        or sorted(target_focus_terms - _FALLBACK_STOPWORDS)
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
    target = named[0] if named is not None else None
    if target is None and target_terms:
        if metric_first:
            target = _preferred_metric(config, target_terms, target_focus_terms)
            if target is None:
                target = _preferred_measure(config, target_terms, target_focus_terms)
        else:
            target = _preferred_measure(config, target_terms, target_focus_terms)
            if target is None:
                target = _preferred_metric(config, target_terms, target_focus_terms)
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
    if named is None and not metric_first:
        # A measure the question names in full ("item revenue") is not the shorter one it
        # shares a word with ("revenue"), but a name that only sits inside other words
        # ("large order revenue") leaves the ordinary target alone.
        target = _named_measure(config, target_focus or text, target) or target

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
        # A grouping that names this clock ("by order date") is the time axis, not a dimension.
        # The grouping above, without the clock, only picked measure or metric.
        role = _object_by_id(config.temporal_roles, temporal_role)
        clock = str(getattr(role, "label", "") or "")
        group_by = _maybe_group_by(config, text, target_terms=target_terms, clock=clock)
        time_spec = _time_spec(temporal_role, text, clock)
        if (
            not _explicit_grain(text, clock)
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
        "version": 1,
        "select": [{"as": select_alias, "expression": expression}],
    }
    if time_spec is not None:
        query["time"] = time_spec

    if group_by:
        query["group_by"] = group_by
    query = _normalize_value_filters(query, _matched_value_rows(runtime, query, text), text=text)
    # The governed metric over the chosen measure answers instead ("how many stores were
    # active" means Active stores, not the all-kinds count it filters).
    governed = _governed_target(config, target_focus or text, query)
    if (
        governed is not None
        and (query.get("time") or {}).get("temporal_role", "") == governed.temporal_role
    ):
        target, target_id, is_measure = governed, str(governed.id), False
        select_alias = _semantic_token(target_id, fallback="value")
        query["select"] = [{"as": select_alias, "expression": {"metric": target_id}}]
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
