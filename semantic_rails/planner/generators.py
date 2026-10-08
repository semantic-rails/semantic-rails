"""Planner-owned fallback candidate generators.

The pattern registry handles named, high-confidence intent shapes. This
module owns the generic catalog fallback: pick the best governed object
for an intent, preserve the caller's partial Query IR, add inferred
grouping / time / value filters, and return planner drafts for
validation/ranking.

This module owns the fallback policy and the small amount of IR synthesis
needed when no named pattern matches. It still calls metadata surfaces such
as ``discover`` and shared catalog helpers, but the candidate-selection
policy itself lives with the planner.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ._base import RuntimeCompositionDraft
from .plan_query import _append_unique_dicts
from .visibility import (
    discovery_query,
    visible_dimensions,
    visible_value_domains,
    with_dimension_visibility,
)

_RANK_COUNT_RE = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)"


@with_dimension_visibility
def fallback_drafts(
    runtime: Any,
    *,
    intent: str,
    partial_query: dict[str, Any] | None = None,
    limit: int = 3,
) -> list[tuple[RuntimeCompositionDraft, str]]:
    """Return generic catalog fallback drafts for an intent.

    The first draft mirrors the old single-candidate fallback. Additional
    drafts come from the top discovered measures/metrics so ``detail=full``
    can expose alternatives.
    """

    from ..metadata import (  # noqa: WPS433 - shared metadata helpers
        _apply_intent_priority_adjustments,
        _infer_stage,
        discover_payload,
    )

    partial = _query_part(dict(partial_query or {}))
    text = str(intent or "").lower()
    out: list[tuple[RuntimeCompositionDraft, str]] = []
    seen_ids: set[str] = set()
    seen_queries: set[str] = set()

    primary = _choose_object_for_terms(runtime, intent, partial, prefer_metric=False)
    if primary:
        draft = _draft_for_choice(runtime, intent=text, partial_query=partial, choice=primary)
        out.append((draft, "catalog_fallback"))
        seen_ids.add(str(primary.get("id", "")))
        seen_queries.add(json.dumps(draft.query, sort_keys=True, default=str))

    if len(out) >= max(1, int(limit or 1)):
        return out

    target_focus = _target_focus_text(intent)
    discovery = discover_payload(
        runtime,
        terms=target_focus or intent,
        partial_query=discovery_query(partial),
        stage=_infer_stage(partial, "", target_focus or intent),
        limit=max(12, int(limit or 1) * 4),
    )
    metrics, _ = _apply_intent_priority_adjustments(
        list(discovery.get("metrics", []) or []), intent=target_focus or intent
    )
    measures, _ = _apply_intent_priority_adjustments(
        list(discovery.get("measures", []) or []), intent=target_focus or intent
    )
    choices = [*metrics, *measures]
    choices.sort(
        key=lambda row: (
            -_intent_match_signal(runtime, intent=target_focus or intent, primary_object=row),
            -float(row.get("score", 0.0) or 0.0),
            str(row.get("id", "")),
        )
    )

    for choice in choices:
        if len(out) >= max(1, int(limit or 1)):
            break
        object_id = str(choice.get("id", "") or "")
        if not object_id or object_id in seen_ids or choice.get("blocked_reason"):
            continue
        draft = _draft_for_choice(runtime, intent=text, partial_query=partial, choice=choice)
        query_key = json.dumps(draft.query, sort_keys=True, default=str)
        if query_key in seen_queries:
            continue
        out.append((draft, "catalog_fallback"))
        seen_ids.add(object_id)
        seen_queries.add(query_key)

    return out


def blocked_object_not_found(intent: str) -> dict[str, Any]:
    """Structured planner block when fallback cannot resolve a target."""

    return {
        "code": "OBJECT_NOT_FOUND",
        "message": "could not resolve a relevant measure or metric",
        "details": {"intent": intent},
        "recovery_hints": [
            {
                "kind": "discover",
                "message": "Call discover with narrower business terms, then plan again with a partial query.",
            }
        ],
    }


@with_dimension_visibility
def _draft_for_choice(
    runtime: Any,
    *,
    intent: str,
    partial_query: dict[str, Any],
    choice: dict[str, Any],
) -> RuntimeCompositionDraft:
    from ..metadata import _select_expr_for_choice
    from ..metadata_parts.object_metadata import _public_object_type  # noqa: WPS433

    query = _query_part(partial_query)
    query.setdefault("version", 1)
    existing_select = list(query.get("select", []) or [])
    planned_select = _select_expr_for_choice(runtime, choice)
    query["select"] = _append_unique_dicts(existing_select, [planned_select])

    existing_group_by = list(query.get("group_by", []) or [])
    inferred_group_by = _choose_group_dimensions(runtime, query, intent)
    group_by = list(dict.fromkeys([*existing_group_by, *inferred_group_by]))
    if group_by:
        query["group_by"] = group_by

    if "time" not in query:
        query = _apply_time_from_text(runtime, query, intent, [str(choice["id"])])

    query = _normalize_value_filters(
        query, _matched_value_rows(runtime, query, intent), text=intent
    )

    resolved = [
        {
            "id": choice["id"],
            "object_type": _public_object_type(choice["kind"]),
            "label": choice["label"],
        }
    ]
    return RuntimeCompositionDraft(
        query=query,
        resolved=resolved,
        rationale=list(choice.get("match_reasons", []) or [])
        or ["resolved the best governed measure or metric from catalog discovery"],
        interpreted_intent={
            "pattern": "catalog_fallback",
            "source": "plan",
            "target": choice["id"],
            "target_kind": choice["kind"],
        },
        score=0.25,
    )


def _normalize_value_filters(
    query: dict[str, Any], matched_values: list[dict[str, Any]] | None = None, *, text: str = ""
) -> dict[str, Any]:
    """Fold one contiguous value phrase; preserve separate clauses and caller filters."""

    where = [
        {**row, "field": row["field"].strip()}
        if isinstance(row, dict) and isinstance(row.get("field"), str)
        else row
        for row in list(query.get("where", []) or [])
    ]
    named: dict[str, list[dict[str, Any]]] = {}
    for row in matched_values or []:
        rows = named.setdefault(row["dimension_id"].strip(), [])
        value = row["value"]
        if not any(
            type(value) is type(existing["value"]) and value == existing["value"]
            for existing in rows
        ):
            rows.append(row)
    for field, rows in named.items():
        values = [row["value"] for row in rows]
        spans = [row["matched_span"] for row in rows if row.get("matched_span") is not None]
        fold = len(values) > 1 and len(spans) == len(values)
        if fold:
            ordered = sorted(spans)
            # Every gap must be only a list separator. Clause text, overlapping
            # matches or missing provenance cannot prove one value-list phrase.
            fold = all(
                before[1] <= after[0]
                and re.fullmatch(
                    r"\s*(?:,|and|,\s*and|from)\s*", text[before[1] : after[0]].lower()
                )
                is not None
                for before, after in zip(ordered, ordered[1:], strict=False)
            )
        for value in [values] if fold else values:
            normalized = {"field": field, "op": "in" if fold else "=", "value": value}
            if normalized not in where:
                where.append(normalized)
    result = dict(query)
    if where:
        result["where"] = where
    return result


def _choose_object_for_terms(
    runtime: Any, terms: str, partial_query: dict[str, Any], *, prefer_metric: bool = False
) -> dict[str, Any]:
    """Pick the most plausible governed metric/measure for free-form terms."""

    from ..metadata import (  # noqa: WPS433 - shared metadata helpers
        _apply_intent_priority_adjustments,
        _infer_stage,
        discover_payload,
    )

    # Pull a wider band than the historical 5 so the intent-match rerank
    # can pick the unqualified primitive when many compound siblings tie.
    target_focus = _target_focus_text(terms)
    search_terms = target_focus or terms
    discovery = discover_payload(
        runtime,
        terms=search_terms,
        partial_query=discovery_query(partial_query),
        stage=_infer_stage(partial_query, "", search_terms),
        limit=15,
    )

    measures_pre, _ = _apply_intent_priority_adjustments(
        list(discovery.get("measures", []) or []), intent=search_terms
    )
    metrics_pre, _ = _apply_intent_priority_adjustments(
        list(discovery.get("metrics", []) or []), intent=search_terms
    )
    measures_ranked = _rerank_for_text(measures_pre, runtime=runtime, intent=search_terms)
    metrics_ranked = _rerank_for_text(metrics_pre, runtime=runtime, intent=search_terms)
    chosen_metric = metrics_ranked[0] if metrics_ranked else None
    chosen_measure = measures_ranked[0] if measures_ranked else None
    metric_keywords = [
        "average",
        "share",
        "rate",
        "ratio",
        "conversion",
        "utilization",
        "cumulative",
        "rolling",
        "mtd",
        "qtd",
        "ytd",
        "prior",
        "delivered",
    ]
    lowered_target = str(search_terms or "").lower()
    per_metric_requested = bool(
        re.search(
            r"\bper\s+(?!day\b|week\b|month\b|quarter\b|year\b)[a-z0-9_ -]+",
            lowered_target,
        )
    )
    use_metric = bool(
        chosen_metric
        and (
            prefer_metric
            or any(word in lowered_target for word in metric_keywords)
            or per_metric_requested
            or not chosen_measure
            or chosen_metric["score"] >= chosen_measure["score"]
        )
    )
    if use_metric and chosen_measure and not prefer_metric:
        assert chosen_metric is not None
        metric_title = " ".join(
            str(chosen_metric.get(key, "") or "") for key in ("id", "name", "label", "description")
        ).lower()
        specialized_tokens = {
            "growth",
            "ratio",
            "share",
            "average",
            "rate",
            "percent",
            "percentage",
            "conversion",
            "cumulative",
            "rolling",
            "mtd",
            "qtd",
            "ytd",
            "prior",
        }
        if any(token in metric_title for token in specialized_tokens) and not any(
            token in lowered_target for token in specialized_tokens
        ):
            use_metric = False
    chosen = dict((chosen_metric if use_metric else chosen_measure) or {})
    return chosen


def _rerank_for_text(
    rows: list[dict[str, Any]], *, runtime: Any, intent: str
) -> list[dict[str, Any]]:
    if not rows:
        return rows
    top_score = float(rows[0].get("score", 0.0) or 0.0)
    band = 12.0
    tied = [row for row in rows if (top_score - float(row.get("score", 0.0) or 0.0)) <= band]
    rest = [row for row in rows if row not in tied]
    if len(tied) <= 1:
        return rows
    tied_ranked = sorted(
        tied,
        key=lambda row: (
            -_intent_match_signal(runtime, intent=intent, primary_object=row),
            -float(row.get("score", 0.0) or 0.0),
            str(row.get("id", "")),
        ),
    )
    return tied_ranked + rest


def _target_focus_text(intent: str) -> str:
    """Return the likely requested measure/metric phrase.

    This is deliberately domain-neutral: trim grouping, cohort, filter,
    and time clauses from the natural-language prompt so fallback
    ranking does not let qualification words overpower the target slot.
    """

    lowered = str(intent or "").lower()
    if not lowered.strip():
        return ""
    top_by_match = re.search(
        rf"^\s*top\s+(?:{_RANK_COUNT_RE}\s+)?[a-z0-9 _-]+?\s+by\s+([a-z0-9 _-]+?)(?:\s+(?:where|for|from|in|with|during|over|having|who|that)\b|[.?!,;]|$)",
        lowered,
    )
    if top_by_match:
        return " ".join(top_by_match.group(1).strip().split())
    splitters = (
        " grouped by ",
        " group by ",
        " by ",
        " where ",
        " filtered to ",
        " filtered by ",
        " filter to ",
        " for ",
        " from ",
        " with ",
        " who ",
        " that ",
        " having ",
        " during ",
        " over ",
    )
    end = len(lowered)
    for splitter in splitters:
        idx = lowered.find(splitter)
        if idx > 0:
            end = min(end, idx)
    focus = lowered[:end]
    focus = re.sub(r"^(?:show|give|list|what is|what are|which|name)\s+", "", focus).strip()
    focus = re.sub(r"\b(total|sum of|average of)\b", " ", focus).strip()
    return " ".join(focus.split())


def _requested_grouping_terms(text: str) -> list[str]:
    lowered = str(text or "").lower()
    top_by_match = re.search(
        r"^(?:top|highest|lowest)\s+([a-z0-9 _-]+?)\s+by\s+([a-z0-9 _-]+)$", lowered
    )
    if top_by_match:
        raw_terms = re.sub(rf"^\s*{_RANK_COUNT_RE}\s+", "", top_by_match.group(1).strip())
    else:
        by_match = re.search(
            r"\bby ([a-z0-9 _-]+?)(?:\s+(?:where|for|from|in|with|during|over|last|this|current|next|prior|having|who|that)\b|[.?!,;]|$)",
            lowered,
        )
        raw_terms = by_match.group(1).strip() if by_match else ""
    if not raw_terms:
        return []
    return [term.strip() for term in re.split(r"\s*(?:,| and | & )\s*", raw_terms) if term.strip()]


def _is_temporal_grouping_term(term: str) -> bool:
    from ..catalog_search import tokenize_search_value  # noqa: WPS433

    if term in {"day", "week", "month", "quarter", "year", "delivered month", "ordered month"}:
        return True
    temporal_tokens = set(tokenize_search_value(term))
    return bool(
        temporal_tokens
        and temporal_tokens.issubset(
            {
                "day",
                "week",
                "month",
                "quarter",
                "year",
                "time",
                "times",
                "delivered",
                "ordered",
                "prepared",
                "versus",
                "vs",
            }
        )
    )


def _term_matches_value_domain(config: Any, term: str) -> bool:
    from ..catalog_search import tokenize_search_value  # noqa: WPS433

    term_tokens = set(tokenize_search_value(term))
    if not term_tokens:
        return False
    for domain in visible_value_domains(config):
        for row in list(domain.values or []):
            values = [
                str(row.value),
                str(row.label),
                *[str(alias) for alias in list(row.aliases or [])],
            ]
            for value in values:
                value_tokens = set(tokenize_search_value(value))
                if value_tokens and value_tokens.issubset(term_tokens):
                    return True
    return False


def _grouping_term_matches(
    runtime: Any, query: dict[str, Any], term: str, *, limit: int = 5
) -> list[str] | None:
    """Return discovery's matched dimension IDs, or None for a skipped grouping term."""

    from ..metadata import discover_payload  # noqa: WPS433 - shared metadata helper

    visible_ids = {row.id for row in visible_dimensions(runtime._config)}
    if not visible_ids:
        return []
    if _is_temporal_grouping_term(term) or _term_matches_value_domain(runtime._config, term):
        return None
    dim_discovery = discover_payload(
        runtime,
        terms=term,
        partial_query=discovery_query(query),
        kinds=["dimension"],
        stage="post_measure",
        limit=limit,
    )
    return [
        str(row["id"])
        for row in dim_discovery["dimensions"]
        if row["id"] in visible_ids
        and any(
            "label/name matched" in reason
            or "search term matched" in reason
            or "exact name match" in reason
            for reason in row["match_reasons"]
        )
    ]


def _choose_group_dimensions(
    runtime: Any, query: dict[str, Any], text: str, chosen_group_dim: str = ""
) -> list[str]:
    from ..metadata import (  # noqa: WPS433 - shared metadata helpers
        _availability_for_object,
        _selection_context,
    )

    if chosen_group_dim and chosen_group_dim in {
        row.id for row in visible_dimensions(runtime._config)
    }:
        return [chosen_group_dim]
    selection = _selection_context(runtime._config, query)
    group_dims: list[str] = []
    for term in _requested_grouping_terms(text):
        matched_ids = _grouping_term_matches(runtime, query, term)
        if matched_ids is None:
            continue
        chosen = ""
        for dimension_id in matched_ids:
            availability = _availability_for_object(
                runtime._config, selection["root_entity"], dimension_id, "dimension"
            )
            if availability["available"]:
                chosen = dimension_id
                break
        if not chosen and matched_ids:
            chosen = matched_ids[0]
        if chosen:
            group_dims.append(chosen)
    return list(dict.fromkeys(group_dims))


def _apply_time_from_text(
    runtime: Any, query: dict[str, Any], text: str, chosen_ids: list[str]
) -> dict[str, Any]:
    from ..metadata import _object_card  # noqa: WPS433 - shared metadata helper
    from .groupings import _time_spec
    from .time_windows import _time_bounds_from_text

    time_bounds = _time_bounds_from_text(text)
    wants_time = any(
        phrase in text
        for phrase in (
            "over time",
            "historical",
            "history",
            "trend",
            "trending",
            "end-of-month",
            "end of month",
            "by day",
            "by week",
            "by month",
            "by quarter",
            "by year",
            "daily ",
            "weekly ",
            "monthly ",
            "quarterly ",
            "yearly ",
            " each day",
            " each week",
            " each month",
            " each quarter",
            " each year",
        )
    ) or bool(time_bounds)
    if not wants_time:
        return query
    role = ""
    for object_id in chosen_ids:
        card = _object_card(runtime, object_id)
        role = str(card.get("default_temporal_role", "") or "")
        if role:
            break
    if not role:
        return query
    query["time"] = _time_spec(role, text)
    return query


def _matched_value_rows(runtime: Any, query: dict[str, Any], text: str) -> list[dict[str, Any]]:
    from ..catalog_search import tokenize_search_value  # noqa: WPS433
    from ..metadata import (  # noqa: WPS433
        _availability_for_object,
        _config_maps,
        _selection_context,
        discover_payload,
    )

    dimensions = visible_dimensions(runtime._config)
    if not dimensions:
        return []
    candidates: list[dict[str, Any]] = []
    filter_terms: list[str] = []
    lowered_text = str(text or "").lower()
    maps = _config_maps(runtime._config)
    maps = {**maps, "dimensions": {row.id: row for row in dimensions}}
    selection = _selection_context(runtime._config, query)
    selected_text_parts: list[str] = []
    for select in list(query.get("select", []) or []):
        selected_text_parts.append(str(select.get("as", "") or ""))
        expression = dict(select.get("expression", {}) or {})
        object_id = str(expression.get("metric", expression.get("measure", "")) or "")
        selected = maps["metric_recipes"].get(object_id) or maps["measures"].get(object_id)
        if selected is not None:
            selected_text_parts.extend(
                [
                    getattr(selected, "id", ""),
                    getattr(selected, "name", ""),
                    getattr(selected, "label", ""),
                    getattr(selected, "description", ""),
                ]
            )
    selected_text = " ".join(selected_text_parts).lower()

    def _value_context(matched_phrase: str) -> str:
        phrase = re.escape(str(matched_phrase or "").strip().lower())
        if not phrase:
            return ""
        pattern = rf"(?P<context>[a-z0-9 _-]{{1,80}}?)\s+(?:is|are|=|equals|equal to|as)\s+{phrase}s?(?![a-z0-9])"
        match = re.search(pattern, lowered_text)
        if not match:
            return ""
        context = match.group("context").strip()
        parts = re.split(
            r"\b(?:where|with|for|from|in|and|or|by|having|who|that)\b",
            context,
        )
        return parts[-1].strip() if parts else context

    def _dimension_context_score(dim: Any, context: str) -> float:
        stopwords = {"a", "an", "the", "is", "are", "was", "were", "to", "of"}
        context_tokens = set(tokenize_search_value(context)) - stopwords
        if not context_tokens:
            return 0.0
        dim_tokens: set[str] = set()
        for source in (
            getattr(dim, "id", ""),
            getattr(dim, "name", ""),
            getattr(dim, "label", ""),
            getattr(dim, "description", ""),
        ):
            dim_tokens.update(tokenize_search_value(source))
        overlap = context_tokens & dim_tokens
        score = float(len(overlap) * 8)
        label = str(getattr(dim, "label", "") or "").lower()
        normalized_context = " ".join(context_tokens)
        if normalized_context and normalized_context in label:
            score += 12.0
        return score

    for pattern in (
        r"\bfrom ([a-z0-9 _-]+?)(?: by | over time|$)",
        r"\bfor ([a-z0-9 _-]+?)(?: by | over time|$)",
        r"\bin ([a-z0-9 _-]+?)(?: by | over time|$)",
        r"\bwith ([a-z0-9 _-]+?)(?: by | over time|$)",
    ):
        for match in re.finditer(pattern, text):
            filter_terms.append(match.group(1).strip())
    generic_terms = {"month", "week", "year", "quarter"}
    comparative_tokens = {
        "more",
        "less",
        "greater",
        "fewer",
        "over",
        "under",
        "than",
        "made",
        "did",
        "purchase",
        "purchases",
        "order",
        "orders",
        "volume",
        "count",
        "counts",
        "total",
        "sum",
        "average",
        "amount",
        "value",
        "balance",
    }
    seen_value_keys = set()

    def _add_candidate(row: dict[str, Any], *, match_key: str, context: str = "") -> None:
        dim_id = str(row.get("dimension_id", "") or "")
        dim = maps["dimensions"].get(dim_id)
        if not dim_id or dim is None:
            return
        availability = _availability_for_object(
            runtime._config, selection["root_entity"], dim_id, "dimension"
        )
        if not availability["available"]:
            return
        enriched = dict(row)
        enriched["_match_key"] = match_key or str(row.get("value", "") or "")
        enriched["_selection_score"] = float(row.get("score", 0.0) or 0.0) + (
            _dimension_context_score(dim, context)
        )
        candidates.append(enriched)

    for filter_term in filter_terms:
        if filter_term in generic_terms:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(filter_term)}s?(?![a-z0-9])", selected_text):
            continue
        tokens = tokenize_search_value(filter_term)
        if not tokens or len(tokens) > 3:
            continue
        if any(token.isdigit() for token in tokens):
            continue
        if any(token in comparative_tokens for token in tokens):
            continue
        value_discovery = discover_payload(
            runtime,
            terms=filter_term,
            partial_query=discovery_query(query),
            kinds=["dimension_value"],
            stage="post_dimension",
            limit=5,
        )
        for row in value_discovery["dimension_values"]:
            if row["score"] < 24.0:
                continue
            if not any(
                reason.startswith("exact name match")
                or reason.startswith("label/name matched")
                or reason.startswith("search term matched")
                for reason in row["match_reasons"]
            ):
                continue
            # Discovery scores the whole filter phrase. Each value must have
            # its own literal/label match, rather than compete under that phrase.
            matched_phrase = ""
            for source in (row["value"], row.get("label")):
                phrase = str(source or "").strip().lower()
                if phrase and re.search(
                    rf"(?<![a-z0-9]){re.escape(phrase)}s?(?![a-z0-9])", lowered_text
                ):
                    matched_phrase = phrase
                    break
            if not matched_phrase:
                continue
            key = (row["dimension_id"], row["value"])
            if key in seen_value_keys:
                continue
            seen_value_keys.add(key)
            _add_candidate(row, match_key=matched_phrase, context=_value_context(matched_phrase))
    for domain in visible_value_domains(runtime._config):
        for value in list(domain.values or []):
            value_phrases = [
                str(value.value),
                str(value.label),
                *[str(alias) for alias in list(value.aliases or [])],
            ]
            matched_phrase = ""
            for phrase in value_phrases:
                normalized_phrase = str(phrase or "").strip().lower()
                if not normalized_phrase:
                    continue
                if re.search(
                    rf"(?<![a-z0-9]){re.escape(normalized_phrase)}s?(?![a-z0-9])", selected_text
                ):
                    continue
                pattern = rf"(?<![a-z0-9]){re.escape(normalized_phrase)}s?(?![a-z0-9])"
                if re.search(pattern, lowered_text):
                    matched_phrase = normalized_phrase
                    break
            if not matched_phrase:
                continue
            context = _value_context(matched_phrase)
            for dim_id in list(domain.dimensions or []):
                dim = maps["dimensions"].get(dim_id)
                if dim is None:
                    continue
                value_key = (dim_id, value.value)
                if value_key in seen_value_keys:
                    continue
                seen_value_keys.add(value_key)
                _add_candidate(
                    {
                        "dimension_id": dim_id,
                        "value": value.value,
                        "label": value.label,
                        "score": 30.0,
                        "match_reasons": [f"value domain matched '{matched_phrase}'"],
                    },
                    match_key=matched_phrase,
                    context=context,
                )
    best_by_match: dict[str, dict[str, Any]] = {}
    for row in candidates:
        match_key = str(row.get("_match_key", "") or row.get("value", "") or "")
        current = best_by_match.get(match_key)
        if current is None or (
            float(row.get("_selection_score", 0.0) or 0.0),
            float(row.get("score", 0.0) or 0.0),
            str(row.get("dimension_id", "")),
        ) > (
            float(current.get("_selection_score", 0.0) or 0.0),
            float(current.get("score", 0.0) or 0.0),
            str(current.get("dimension_id", "")),
        ):
            best_by_match[match_key] = row
    out = []
    for row in best_by_match.values():
        cleaned = {key: value for key, value in row.items() if not key.startswith("_")}
        for source in (row["_match_key"], row["value"], row.get("label")):
            if source is None or not str(source).strip():
                continue
            phrase = re.escape(str(source).strip().lower())
            matches = list(re.finditer(rf"(?<![a-z0-9]){phrase}s?(?![a-z0-9])", lowered_text))
            if matches:
                if len(matches) == 1:
                    cleaned["matched_span"] = matches[0].span()
                break
        out.append(cleaned)
    out.sort(
        key=lambda row: (-float(row.get("score", 0.0) or 0.0), str(row.get("dimension_id", "")))
    )
    return out[:3]


def _intent_match_signal(
    runtime: Any,
    *,
    intent: str,
    primary_object: dict[str, Any] | None,
) -> float:
    """Return a 0..1 score that differentiates candidates for an intent."""

    from ..catalog_search import derive_discovery_tokens, tokenize_search_value  # noqa: WPS433

    if not primary_object:
        return 0.0
    object_id = str(primary_object.get("id", "") or "")
    label = str(primary_object.get("label", "") or "")
    name = str(primary_object.get("name", "") or "")
    description = str(primary_object.get("description", "") or "")
    derived_terms = derive_discovery_tokens(name, label, description)
    intent_tokens = set(tokenize_search_value(intent))
    if not intent_tokens:
        return 0.0
    haystack_tokens: set[str] = set()
    for source in (label, name, object_id):
        haystack_tokens.update(tokenize_search_value(source))
    haystack_tokens.update(derived_terms)
    if not haystack_tokens:
        return 0.0
    overlap = intent_tokens & haystack_tokens
    overlap_ratio = len(overlap) / float(len(intent_tokens))
    score = 0.45 * overlap_ratio
    lowered_intent = str(intent or "").lower()
    label_phrase = re.sub(r"\s*\(.*?\)\s*", "", label).strip().lower()
    phrase_matches: list[str] = []
    if label_phrase and len(label_phrase) >= 4 and label_phrase in lowered_intent:
        phrase_matches.append(label_phrase)
    if phrase_matches:
        longest = max(len(phrase) for phrase in phrase_matches)
        score += min(0.5, 0.25 + 0.02 * longest)
    # Both ranking callers break equal scores by discovery score and object id.
    # A randomized hash here would reorder candidates before those stable ties.
    return max(0.0, min(1.0, score))


def _query_part(payload: dict[str, Any]) -> dict[str, Any]:
    """Return only the Query IR part of a mixed request envelope."""

    out = dict(payload or {})
    out.pop("policy_context", None)
    out.pop("request_context", None)
    out.pop("request_id", None)
    return out


__all__ = ["blocked_object_not_found", "fallback_drafts"]
