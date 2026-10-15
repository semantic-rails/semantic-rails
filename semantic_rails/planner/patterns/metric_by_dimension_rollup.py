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

from ...query_call import TIME_AXIS
from .._base import (
    _NAME_CONNECTORS,
    _TERM_SYNONYMS,
    RuntimeCompositionDraft,
    _aggregation_from_text,
    _declared_name_forms,
    _named_metric,
    _object_by_id,
    _preferred_measure,
    _preferred_metric,
    _resolved,
    _singular,
    _tokens,
)
from ..call import Call, Order, QueryBuilder, Time, condition
from ..coverage import CoverageGap, _coverage_why
from ..generators import _matched_value_rows, _normalize_value_filters, _target_focus_text
from ..groupings import (
    _display_entities,
    _entity_grouping,
    _entity_stand_ins,
    _explicit_grain,
    _maybe_group_by,
    _named_run,
    _names_whole_entity,
    _time_spec,
)
from ..intent_ir import _FALLBACK_STOPWORDS
from ..qualifiers import _add_order, _target_measure_terms, _top_n_intent
from ..ranking_checks import _dimension_nouns, _ranking_request, _ranking_words, _superlative
from ..time_windows import _time_bounds_from_text, _time_window
from ._protocol import IntentPattern


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


def _named_by_rows(target: Any, noun: str, question: str) -> bool:
    """Whether the only words naming the subject are the ranked rows' noun.

    "top 5 customers" and "the 5 customers who spent the most" name no value to rank by: the
    word that picked Customers to count names the rows, and every row counts one.
    """

    rows = {_singular(word) for word in _tokens(noun)}
    said = {_singular(word) for word in _tokens(question)} - _FALLBACK_STOPWORDS
    names = {_singular(word) for name in _declared_name_forms(target) for word in _tokens(name)}
    return bool(names & said) and names & said <= rows


def _listed(
    runtime: Any, question: str, query: Call, target: Any
) -> tuple[list[str], dict[str, Any]]:
    """The stand-ins of what a clause opening with "which" or "who" lists, or why plan asks.

    "which" lists the entity named right after it ("which accounts"), unless that noun is the
    only word naming ``target``, the draft's subject ("which stores are there" would list the
    stores the open store count counts): as for a ranking, nothing is listed. "who" lists the
    one entity with a display the draft's subject reaches, when its clause names no entity: in
    "who placed an order" the order may be what was placed, so nothing is listed. Several such
    entities ask which, naming them, with no call to run: one built here would miss what plan
    adds to the draft later (filters, the governed metric, a balance's day). None, or a term
    naming no single entity, lists nothing (the answer-shape check holds the draft). A
    superlative no ranking reads ("who had the most MRR") lists nothing either.
    """

    from ..answer_shape import _clause_entity, _list_clauses  # noqa: WPS433 - imports patterns

    config = runtime._config
    words = [word for word, _start, _end in _ranking_words(question)]
    if any(_superlative(words, index) for index in range(len(words))):
        return [], {}
    lowered = question.lower()
    windows = list(_time_window(question).spans)
    for word, clause in _list_clauses(config, question):
        if word not in {"which", "who", "whom"}:
            continue
        if word == "which":
            run = _named_run(config, lowered, clause[0])
            noun = lowered[run[0] : run[1]] if run else ""
            entity = _entity_grouping(config, noun) if run else None
            if entity is None or _named_by_rows(target, noun, question):
                return [], {}
            return entity[1], {}
        if _clause_entity(config, lowered, windows, clause) is not None:
            return [], {}
        entities = _display_entities(runtime, query)
        if len(entities) != 1:
            return [], _whose_rows_why(word, query, entities) if entities else {}
        return [ids for part in _entity_stand_ins(config, entities[0]) for ids in part], {}
    return [], {}


def _as_list(config: Any, query: Call, listed: list[str]) -> Call:
    """``query`` listing the rows of ``listed`` (an entity's stand-ins, the name last): grouped
    by them, keeping only the rows whose value is known and isn't 0 (a member has a value:
    ``not`` alone keeps unknown ones), ordered by the name, then by period and the other
    groupings."""

    builder = QueryBuilder(config, query).group(*listed)
    group_by = builder.build().group_by
    first = listed[-1]
    value = query.select[0].alias
    return (
        builder.having([condition(value, "missing", False), condition(value, "not", 0)])
        .orders(
            [
                Order(first),
                *([Order(TIME_AXIS)] if query.time is not None and query.time.grain else []),
                *(Order(item) for item in group_by if item != first),
            ]
        )
        .build()
    )


def _whose_rows_why(word: str, query: Call, entities: list[Any]) -> dict[str, Any]:
    """Why plan asks whose rows "who" lists when the subject reaches several entities with a
    display: the entities, and no call (``_listed``)."""

    labels = ", ".join(str(row.label) for row in entities)
    options = [{"entity": row.id, "label": row.label} for row in entities]
    gap = CoverageGap(
        kind="list_unrealized",
        clause=f'"{word}"',
        message=(
            f'The question asks "{word}", and what it counts reaches several entities with a '
            f"display name ({labels}), so plan doesn't pick whose rows to list."
        ),
        expected={"answer": "rows", "entities": [row.id for row in entities]},
        recovery_hint={
            "kind": "ask_whose_rows",
            "message": 'Ask the user which they mean, then plan again naming it ("which '
            'accounts …").',
        },
    )
    why = _coverage_why([gap]) or {}
    clarification = {"question": f"Whose rows: {labels}?", "options": options}
    return {**why, "details": {**why.get("details", {}), "clarification": clarification}}


def _match(runtime: Any, text: str, terms: set[str]) -> RuntimeCompositionDraft | None:
    config = runtime._config
    question = text
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
    group_by = _maybe_group_by(config, text, target_terms=target_terms, each=True)
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
    first, second = (
        (_preferred_metric, _preferred_measure)
        if metric_first
        else (_preferred_measure, _preferred_metric)
    )
    if target is None and target_terms:
        target = first(config, target_terms, target_focus_terms) or second(
            config, target_terms, target_focus_terms
        )
    if target is None:
        target = first(config, terms) or second(config, terms)
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
    time_spec: Time | None = None
    temporal_role = str(getattr(target, "default_temporal_role", "") or "") or str(
        getattr(target, "temporal_role", "") or ""
    )
    if temporal_role:
        # A grouping that names this clock ("by order date") is the time axis, not a dimension.
        # The grouping above, without the clock, only picked measure or metric.
        role = _object_by_id(config.temporal_roles, temporal_role)
        clock = str(getattr(role, "label", "") or "")
        group_by = _maybe_group_by(config, text, target_terms=target_terms, clock=clock, each=True)
        time_spec = _time_spec(temporal_role, text, clock)
        if (
            not _explicit_grain(text, clock)
            and not _time_window(lowered).relative_unit  # e.g. "last 7 days", "today"
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
    builder = QueryBuilder(config)
    if is_measure:
        builder.measure(target_id, _aggregation_from_text(text, terms, target))
    else:
        builder.select(target_id)
    builder.time(time_spec)

    # A ranking keeps what the ranking parser reads: its count ("top three"), its direction
    # ("bottom", "least") and the entity it ranks ("which 2 accounts had the most MRR").
    request = _ranking_request(question, _dimension_nouns(config))
    direction = "desc"
    held: dict[str, Any] = {}
    if request is not None and request["direction"] and (is_top or request["limit"] is not None):
        is_top, direction = True, str(request["direction"]).lower()
        top_n = request["limit"] if request["limit"] is not None else top_n
        ranked = _entity_grouping(config, str(request["noun"]))
        if ranked is not None and not _named_by_rows(target, str(request["noun"]), question):
            group_by = list(dict.fromkeys([*group_by, *ranked[1]]))
        noun = [row for row in config.entities if _names_whole_entity(str(request["noun"]), row)]
        if ranked is None and len(noun) == 1:  # a row-grain or composite key: no rows to rank
            message = (
                f"{noun[0].label} has no single key to rank by; rank by one of its dimensions."
            )
            held, is_top = {"code": "PLAN_RANKING_NO_KEY", "message": message}, False
    builder.groups(group_by)
    listed, ask = ([], {}) if is_top else _listed(runtime, question, builder.build(), target)
    drafted = builder.build()
    builder = QueryBuilder(
        config,
        _normalize_value_filters(
            drafted,
            _matched_value_rows(runtime, drafted, question),
            text=question,
            config=config,
        ),
    )
    if is_top:
        builder.order(drafted.select[0].alias, direction).limit(top_n)
    elif not listed:
        _add_order(builder)
    # A list keeps the rows the subject counts, by name.
    query = _as_list(config, builder.build(), listed) if listed else builder.build()
    group_by = list(query.group_by)

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
            # A list's own having says which rows are members (``_as_list``).
            **({"lists": listed} if listed else {}),
        },
        score=_CATCH_ALL_SCORE,
        blocked_reason=held or ask,
    )


PATTERN = IntentPattern(
    name="metric_by_dimension_rollup",
    match=_match,
)
