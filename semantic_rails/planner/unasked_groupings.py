"""Readiness: groupings and grains the draft adds that the question didn't ask for."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

from ._base import _object_by_id, _singular
from .grouping_checks import _entity_grouping_dimensions, _query_clocks, _reads_grouping, _time_of
from .groupings import _listed_grouping_terms, _named_run
from .plan_query import _where_filters
from .ranking_checks import _dimension_nouns, _ranking_request
from .time_phrases import _TIME_UNITS, _names_time_axis
from .time_windows import _time_window


def _grain_bucket(day: date, grain: str) -> Any:
    """The calendar bucket of a day at a grain; weeks start on Monday, as the engine's do."""

    if grain == "week":
        return day - timedelta(days=day.weekday())
    if grain == "month":
        return day.year, day.month
    if grain == "quarter":
        return day.year, (day.month - 1) // 3
    if grain == "year":
        return day.year
    return day


def _grain_splits(time: dict[str, Any]) -> bool:
    """Whether the time block's grain can put the rows in two or more buckets.

    It can't when the block's window fits in one bucket: a calendar window inside one period of
    the grain ("in Q1 2017" at quarter or year), or the last single period of the grain ("last
    month" at month), or of a day. Any other grain, an open or relative window of more periods,
    or a non-Gregorian calendar may split them.
    """

    grain = str(time.get("grain") or "")
    if not grain:
        return False
    if grain not in _TIME_UNITS or str(time.get("calendar_id") or "default") != "default":
        return True
    window = time.get("range")
    if isinstance(window, dict):
        last = window.get("last")
        return not (
            isinstance(last, dict) and last.get("value") == 1 and last.get("unit") in {grain, "day"}
        )
    try:
        start = datetime.fromisoformat(str(time["start"]))
        end = datetime.fromisoformat(str(time["end"]))
    except (KeyError, ValueError):
        return True
    final = max((end - timedelta(microseconds=1)).date(), start.date())
    return _grain_bucket(start.date(), grain) != _grain_bucket(final, grain)


def _without_windows(question: str) -> str:
    """The lowercase question with every time window it states blanked out."""

    lowered = str(question or "").lower()
    for start, end in _time_window(question).spans:
        lowered = lowered[:start] + " " * (end - start) + lowered[end:]
    return lowered


# A series the question asks for in words: it splits the answer by time at plan's grain.
_SERIES_RE = re.compile(r"\b(?:over\s+time|trends?|trending|time\s+series)\b")


def _asks_grain(question: str, grains: Iterable[str]) -> bool:
    """Whether the question's own words, outside every time window it states, ask for one of
    the grains' buckets: its unit or "-ly" form ("by month", "monthly", "month level", "per
    week", "daily"), or a series ("over time", "trend", "trending", "time series"), which plan
    buckets at its default grain."""

    lowered = _without_windows(question)
    forms = {
        form
        for grain in grains
        for form in (grain, f"{grain}s", "daily" if grain == "day" else f"{grain}ly")
    }
    return bool(forms & set(re.findall(r"[^\W\d_]+", lowered)) or _SERIES_RE.search(lowered))


def _names_grain(config: Any, question: str, query: dict[str, Any], grain: str) -> bool:
    """Whether the question asks for the grain's buckets (``_asks_grain``) or, for days, lists
    a grouping that names the query's clock ("by order date")."""

    if _asks_grain(question, (grain,)):
        return True
    clocks = _query_clocks(config, query)
    return grain == "day" and any(
        _names_time_axis(term, clock)
        for term in _listed_grouping_terms(question, config)
        for clock in clocks
    )


# "revenue per store" and "revenue for each store" are "revenue by store": the words after
# "per", "each" or "every", up to a clause.
_PER_GROUPING_RE = re.compile(
    r"\b(?:per|each|every)\s+([a-z _-]+?)"
    r"(?=\s+(?:by|and|where|for|from|in|with|during|over|having|who|that)\b|\s*[.?!,;]|\s*$)"
)


def _asked_grouping_terms(config: Any, question: str, listed: Iterable[str] = ()) -> list[str]:
    """What the question asks to group by: each grouping it lists (``_listed_grouping_terms``),
    the noun a ranking ranks ("which 5 stores had the most orders"), the words after "per",
    "each" or "every" ("revenue per store"; "each plan" in "did each plan make", where a name
    ends), and ``listed``: what a clause opening with "which" or "who" lists
    (``answer_shape._listed_entity_terms``). Windows are not part of any of them."""

    request = _ranking_request(question, _dimension_nouns(config))
    lowered = _without_windows(question)
    each: list[str] = []
    for match in _PER_GROUPING_RE.finditer(lowered):
        run = _named_run(config, lowered, match.start(1))
        named = run is not None and run[1] <= match.end(1)
        each.append(lowered[run[0] : run[1]] if run and named else match.group(1).strip())
    return [
        *_listed_grouping_terms(question, config),
        *([str(request["noun"])] if request else []),
        *each,
        *listed,
    ]


def _unasked_grouping_why(
    runtime: Any,
    question: str,
    query: dict[str, Any],
    partial_query: dict[str, Any] | None = None,
    listed: Iterable[str] = (),
) -> dict[str, Any] | None:
    """Every grouping the draft adds traces to the question, or the plan is not ready.

    A group_by dimension traces when a grouping the question asks for reads it
    (``_asked_grouping_terms``, with ``listed``, what its "which" and "who" clauses list; read
    as ``_dropped_grouping_why`` reads a listed one), when the caller's ``partial_query``
    group_by has it, when the draft's own ``=`` or ``IN`` filter
    keeps only values of it the question names, or when it is the clock's own date dimension
    beside a day grain on that clock: it adds no row (a metric constraint may require it on a
    balance, ``snapshot.shape_snapshot``). The time block's grain traces when the
    question's words outside its windows name it (``_names_grain``), when the caller's
    ``partial_query`` time has it, or when it can't split the rows because the window fits in
    one bucket (``_grain_splits``). The package declares no default grain, so a grain plan
    picks for a comparison, a year-over-year shift or a window of several periods doesn't
    trace.

    A ranking must then keep the top N of the entity it ranks (``_ranking_why``). The check
    only holds a plan; it never changes the draft.
    """

    config = runtime._config
    caller = partial_query or {}
    time = _time_of(query)
    grain = str(time.get("grain") or "")
    splits = _grain_splits(time)
    grain_traced = not splits or (
        _time_of(caller).get("grain") == grain or _names_grain(config, question, query, grain)
    )
    terms = _asked_grouping_terms(config, question, listed)
    stand_ins = [_entity_grouping_dimensions(config, term) for term in terms]
    # A dimension the draft filters to the values the question names splits the rows into
    # those values only; the filter-value check holds a filter that keeps any other.
    pinned = {
        str(row["field"])
        for row in _where_filters(query)
        if (row.get("op") == "=" and not isinstance(row.get("value"), (list, tuple, dict)))
        or (str(row.get("op")).lower() == "in" and isinstance(row.get("value"), list))
    }
    chosen = set(caller.get("group_by") or [])
    clock = _object_by_id(config.temporal_roles, str(time.get("temporal_role") or ""))
    clock_day = str(getattr(clock, "dimension", "") or "") if grain == "day" else ""
    grouped = [
        row
        for item in dict.fromkeys(query.get("group_by") or [])
        if (row := _object_by_id(config.dimensions, item)) is not None
    ]
    unasked = [
        row
        for row in grouped
        if row.id not in chosen
        and row.id not in pinned
        and not (row.id == clock_day and row.data_type == "date")
        and not any(
            _reads_grouping(term, ids, row) for term, ids in zip(terms, stand_ins, strict=True)
        )
    ]
    if unasked or not grain_traced:
        names = [str(row.label or row.id) for row in unasked] + ([] if grain_traced else [grain])
        return {
            "code": "PLAN_UNASKED_GROUPING",
            "message": (
                f"The draft groups by {', '.join(names)}, which the question never asks for, "
                "so it splits the answer into more rows than asked and plan doesn't call it "
                "ready."
            ),
            "details": {
                "unasked_groupings": names,
                **({"dimensions": [row.id for row in unasked]} if unasked else {}),
                **({} if grain_traced else {"grain": grain}),
            },
            "recovery_hints": [
                {
                    "kind": "remove_unasked_grouping",
                    "message": (
                        "Remove them from best.query_ir (a dimension from group_by; the grain "
                        "from time, or the whole time block and its order_by entry when it "
                        "holds no start, end or range), then validate; or ask again naming "
                        'the grouping you want ("by month", "monthly").'
                    ),
                }
            ],
        }
    order_by = [row for row in query.get("order_by") or [] if isinstance(row, dict)]
    aliases = {row.get("as") for row in query.get("select") or [] if isinstance(row, dict)}
    aliases.discard(None)
    if not (
        grouped
        and query.get("limit") is not None
        and order_by
        and order_by[0].get("field") in aliases
    ):
        return None
    return _ranking_why(runtime, question, query, partial_query, grouped, splits)


def _ranking_why(
    runtime: Any,
    question: str,
    query: dict[str, Any],
    partial_query: dict[str, Any] | None,
    grouped: list[Any],
    splits: bool,
) -> dict[str, Any] | None:
    """A ranking keeps the top N of the entity it ranks, or the plan is not ready.

    The draft keeps the top N of its group_by rows, split by its grain when that can split
    them. Those rows are the entity the question ranks only when the noun it ranks
    (``_ranking_request``) is not a time unit and reads every group_by dimension, as
    ``_unasked_grouping_why`` reads an asked grouping (an entity's key and its label), or,
    when the question ranks nothing, when the caller's ``partial_query`` states the ranking
    over its own group_by. Else the draft may keep the top N (store, customer type) pairs, or
    (month, store) rows. A ranking of the entity the question ranks, split by a grain, may mean
    the top N over the whole window or the top N in each period. Each is held with no runnable
    option, and the hint asks which ranking is meant.
    """

    config = runtime._config
    request = _ranking_request(question, _dimension_nouns(config))
    noun = str(request["noun"]) if request else ""
    stand_ins = _entity_grouping_dimensions(config, noun) if noun else None
    ranks_entity = (
        bool(noun)
        and _singular(noun) not in _TIME_UNITS
        and all(_reads_grouping(noun, stand_ins, row) for row in grouped)
    )
    keys = [row.id for row in grouped]
    # A question that ranks nothing leaves the ranking to a caller that states it: its limit,
    # over its own group_by.
    caller = partial_query or {}
    callers = (
        not noun
        and caller.get("limit") is not None
        and set(keys) <= set(caller.get("group_by") or [])
    )
    if not splits and (ranks_entity or callers):
        return None
    entity = noun or " and ".join(str(row.label or row.id) for row in grouped)
    limit = int(query["limit"])
    grain = str(_time_of(query).get("grain") or "") if splits else ""
    rows = ", ".join([str(row.label or row.id) for row in grouped] + ([grain] if grain else []))
    # Held here, a ranking of the entity the question ranks is split by its grain.
    readings = (
        f" The top {limit} {entity} over the whole window, or the top {limit} {entity} in "
        f"each {grain}?"
        if ranks_entity
        else ""
    )
    return {
        "code": "PLAN_RANKING_PERIOD_AMBIGUOUS",
        "message": (
            f"The question ranks {entity}, but the draft keeps the top {limit} ({rows}) rows, "
            f"which may not be the top {limit} {entity}, so plan doesn't call it ready."
            f"{readings}"
        ),
        "details": {"limit": limit, "ranked": keys, **({"grain": grain} if grain else {})},
        "recovery_hints": [
            {
                "kind": "ask_which_ranking",
                "message": (
                    "Ask the user which ranking they mean, then plan again with a question "
                    "that names it."
                ),
            }
        ],
    }
