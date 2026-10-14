"""Why a draft is held for time, currency or value words it doesn't carry."""

from __future__ import annotations

import re
from typing import Any

import yaml

from ..errors import SemanticLayerError
from ..naming import last_token
from ..period_completeness import incomplete_period_why
from ._base import _object_by_id, _runtime_composition_terms
from .call import Call, QueryBuilder
from .groupings import _requested_grouping_terms
from .intent_ir import IntentIR
from .time_reference import time_policy_context
from .time_windows import _time_window

# Window forms the planner can resolve from natural language. Surfaced
# verbatim in recovery hints when a temporal phrase fails to resolve.
_SUPPORTED_WINDOW_FORMS = (
    "last N days/weeks/months/quarters/years (e.g. 'last 7 days')",
    "last/past/previous <day|week|month|quarter|year> (e.g. 'last month')",
    "this/current <week|month|quarter|year>",
    "yesterday / today",
    "a calendar year after in/for/during (e.g. 'in 2017'), or consecutive years ('2016 and 2017')",
    "a quarter or half with a year (e.g. 'Q2 2017', 'second quarter of 2017', 'H1 2017')",
    "a month with a year, or a month range (e.g. 'March 2017', 'January 2017 through June 2017')",
    "days with a year, or ISO dates (e.g. 'April 3, 2017', 'April 1 to April 7, 2017', "
    "'2017-04-03')",
    "one completed named month, quarter or half without a year (latest on or before now)",
    "a month and day without a year, optionally with a matching weekday (e.g. 'Wed Sept 30')",
    "since a named month, month/day or quarter, or since a dated one (from that day on)",
    "all time / ever / in total / to date / since launch / since the beginning / since we started",
    "an explicit time window (last, this, or start / end ISO dates) in the plan tool's query",
)


def _pattern_dropped_start(intent: str, query: Call, partial_query: Call | None) -> str:
    """The question's window start a draft left out while keeping its end.

    The lookback retry records the start it drops; a pattern that bounds a
    period comparison drops it itself. Either way plan says so.
    """

    from .time_windows import _time_bounds_from_text  # noqa: WPS433

    if partial_query is not None and partial_query.time is not None and partial_query.time.window:
        return ""
    expected = _time_bounds_from_text(intent)
    time = query.time.window if query.time is not None else {}
    if expected.get("start") and not time.get("start") and time.get("end") == expected.get("end"):
        return str(expected["start"])
    return ""


def _with_time_gap(why: dict[str, Any], time_why: dict[str, Any] | None) -> dict[str, Any]:
    """Add a time why (unresolved window, incomplete period, dropped start) to a coverage-gap
    why."""

    if time_why is None:
        return why
    details = dict(time_why.get("details") or {})
    clause = ", ".join(details.get("unresolved_phrases") or []) or str(
        (details.get("incomplete_period") or {}).get("start") or details.get("requested_start", "")
    )
    gaps = [
        *list((why.get("details") or {}).get("gaps") or []),
        {
            "kind": str(time_why.get("code", "")).lower(),
            "clause": clause,
            "message": str(time_why.get("message", "")),
        },
    ]
    hints = list(why.get("recovery_hints") or [])
    kinds = {str(hint.get("kind", "")) for hint in hints if isinstance(hint, dict)}
    hints += [
        hint
        for hint in list(time_why.get("recovery_hints") or [])
        if isinstance(hint, dict) and str(hint.get("kind", "")) not in kinds
    ]
    return {
        **why,
        "details": {**dict(why.get("details") or {}), "gap_count": len(gaps), "gaps": gaps},
        "recovery_hints": hints,
    }


def _unconsumed_terms_why(terms: list[str]) -> dict[str, Any] | None:
    """Explain a draft that leaves out a number or a clock or zone word of the question."""

    if not terms:
        return None
    return {
        "code": "PLAN_UNMATCHED_TERMS",
        "message": (
            f"The question has numbers or time words the draft doesn't use: {', '.join(terms)}. "
            "It may have dropped an hour, a range or a threshold, so plan doesn't call it ready."
        ),
        "details": {"terms": terms},
        "recovery_hints": [
            {
                "kind": "state_missing_condition",
                "message": (
                    "Add the filter or limit to best.query, or (plan resolves days and "
                    "coarser windows only) state an hour range as query.time start and end "
                    "ISO timestamps, then validate. Asking again without those words changes "
                    "the question."
                ),
            }
        ],
    }


def _unconsumed_catalog_why(question: str, words: list[str]) -> dict[str, Any] | None:
    """Explain a draft that leaves out a question word naming a catalog object.

    A word inside a grouping the question asks for ("by store, customer type and product type")
    means the draft dropped that grouping, and the message says so. A comma in the list reads as
    "and" here: the grouping parse stops at a comma, which is how the draft lost the rest.
    """

    if not words:
        return None
    listed = _requested_grouping_terms(re.sub(r"\s*,\s*(?:and\s+)?", " and ", question))
    dropped = [term for term in listed if set(words) & set(re.findall(r"[^\W_]+", term))]
    terms = words[:8]  # as many as the warning names
    message = (
        f"The draft drops the grouping by {', '.join(dropped)} that the question asks for: "
        if dropped
        else "The draft may answer a different question: "
    ) + (
        f"it doesn't use these words, which name catalog objects: {', '.join(terms)}. "
        "So plan doesn't call it ready."
    )
    return {
        "code": "PLAN_UNMATCHED_TERMS",
        "message": message,
        "details": {"terms": terms, **({"dropped_groupings": dropped} if dropped else {})},
        "recovery_hints": [
            {
                "kind": "use_named_objects",
                "message": (
                    "Find what these words name with discover, add it to best.query (a "
                    "group_by for a grouping, the select for a measure), then validate; or ask "
                    "the user what they mean. They name catalog objects, so asking again "
                    "without them changes the question."
                ),
            }
        ],
    }


def _qualifying_entity_why(runtime: Any, intent_ir: IntentIR, query: Call) -> dict[str, Any] | None:
    """Parsed qualifications remain held until their cohort and scope are proven: an entity
    filter, or a scoped aggregate's predicate, on the qualifying entity says over which rows
    each entity's aggregate is taken."""
    # Entity discovery also fills this slot for ordinary entity mentions.
    # A cohort obligation needs a parsed qualification, not just that hint.
    if intent_ir.qualifying_entity is None or not (
        intent_ir.qualification_phrase or intent_ir.threshold is not None
    ):
        return None
    entity = intent_ir.qualifying_entity.id
    scoped = [
        predicate
        for item in query.select
        if isinstance(item.expression, dict)
        for predicate in item.expression.get("predicates") or []
    ]
    if any(
        row.entity == entity and row.match == "having" and row.over for row in query.entity_filters
    ) or any(predicate.get("entity") == entity and predicate.get("over") for predicate in scoped):
        return None
    return {
        "code": "PLAN_INTENT_COVERAGE_GAP",
        "message": "The draft does not prove the qualifying cohort and its time scope.",
        "details": {"qualifying_entity": intent_ir.qualifying_entity.id},
    }


def _contraction_tail(question: str, term: str) -> bool:
    """Whether the question writes "s" only as the end of a contraction or possessive ("what's",
    "store's"): with it gone, the question means the same. No other tail is: without its "t",
    "can't" says the opposite."""

    if term != "s":
        return False
    lowered = str(question or "").lower()
    found = list(re.finditer(rf"(?<![^\W_]){re.escape(term)}(?![^\W_])", lowered))
    return bool(found) and all(
        re.search(r"[^\W_]['’]$", lowered[: match.start()]) for match in found
    )


# A statistic a question asks of a measure: "median revenue", "latest MRR", "unique visitors".
_STATISTICS = {"median": "median", "latest": "last_value", "unique": "count_distinct"}
_STATISTIC_RE = re.compile(rf"\b({'|'.join(_STATISTICS)})\b")


def _define_metric_why(
    runtime: Any, question: str, query: Call, unconsumed: list[str]
) -> dict[str, Any] | None:
    """A statistic the question asks of a measure that reads at another one, and that no
    metric defines (the draft selects the measure itself): plan holds with the metric's YAML
    ready to paste, and the call that reads the measure at that statistic now when the
    package allows it. Only when the statistic is the one word the draft leaves ``unconsumed``:
    otherwise the subject itself is in doubt."""

    words = sorted(set(_STATISTIC_RE.findall(question.lower())))
    if not words or len(query.select) != 1 or not set(unconsumed) <= set(words):
        return None
    config = runtime._config
    item = query.select[0]
    measure = _object_by_id(config.measures, item.ref)
    if measure is None:
        return None
    read_at = item.aggregation or measure.default_aggregation
    asked = [word for word in words if _STATISTICS[word] != read_at]
    if not asked:
        return None
    word = asked[0]
    aggregation = _STATISTICS[word]
    name = f"{word}_{last_token(measure.id)}"
    metric = {
        "label": f"{word.capitalize()} {measure.label or last_token(measure.id)}",
        "kind": "aggregate",
        **({"value_type": measure.value_type} if measure.value_type else {}),
        **(
            {"temporal_role": measure.default_temporal_role}
            if measure.default_temporal_role
            else {}
        ),
        "expression": {"kind": "aggregate", "measure": measure.id, "aggregation": aggregation},
    }
    details: dict[str, Any] = {
        "measure": measure.id,
        "aggregation": aggregation,
        "yaml": yaml.safe_dump({"metrics": {name: metric}}, sort_keys=False),
    }
    if aggregation in (measure.allowed_aggregations or []):
        override = {"kind": "measure", "measure": measure.id, "aggregation": aggregation}
        components = QueryBuilder(config, query).selects([]).select(override, name).build()
        details["components"] = {"call": components.to_call()}
    return {
        "code": "DEFINE_METRIC",
        "message": (
            f"No metric defines the {word} of {measure.label or measure.id}, which reads at "
            f"{read_at}; plan doesn't answer with a statistic the package hasn't defined."
        ),
        "details": details,
        "recovery_hints": [
            {
                "kind": "define_metric",
                "message": (
                    "Add details.yaml to the package's metrics, or send "
                    "details.components.call to read the measure at that statistic now."
                    if "components" in details
                    else "Add details.yaml to the package's metrics; the measure doesn't "
                    f"allow {aggregation} until its allowed_aggregations include it."
                ),
            }
        ],
    }


def _dropped_value_why(
    question: str, unconsumed: list[str], unresolved: set[str]
) -> dict[str, Any] | None:
    """Unknown words left unresolved by the intent parse make the draft not ready.

    The parse records a word as it normalizes it ("messages" as "message", "sent" as
    "received"), so membership compares that form; the message names the question's spelling.
    The hint offers to ask again without the words only when each is the end of a contraction
    (``_contraction_tail``): plan can't tell whether any other word changes the question.
    """

    unknown = [term for term in unconsumed if _runtime_composition_terms(term) & unresolved]
    if not unknown:
        return None
    filler = all(_contraction_tail(question, term) for term in unknown)
    return {
        "code": "PLAN_UNMATCHED_TERMS",
        "message": (
            f"The question has words that match nothing in the catalog and the draft "
            f"doesn't consume: {', '.join(unknown)}. So plan doesn't call it ready."
        ),
        "details": {"terms": unknown, "kind": "filter_values_unrealized"},
        "recovery_hints": [
            {
                "kind": "add_missing_condition",
                "message": (
                    "Ask again without those words: each ends a contraction or possessive "
                    '("s" in "what\'s"), so the question means the same without them.'
                    if filler
                    else "Find the values with valid_values, add the filter to "
                    "best.query, then validate; or ask the user what these words mean. "
                    "Plan can't tell whether they change the question, so asking again "
                    "without them may answer another one."
                ),
            }
        ],
    }


def _time_assumptions(intent: str, query: Call) -> list[str]:
    """The reading plan took of an open end in the window the draft carries."""

    window = _time_window(intent)
    time = query.time.window if query.time is not None else {}
    if not window.assumptions:
        return []
    if any(time.get(key) != window.bounds.get(key) for key in ("start", "end")):
        return []
    return list(window.assumptions)


def _incomplete_period_why(runtime: Any, query: Call, start: Any = "") -> dict[str, Any] | None:
    """Hold a draft that compares periods until each period it returns has ended at the
    request's ``now`` (``period_completeness``), read as execution reads the draft."""

    call = query.to_call()
    context = dict(time_policy_context() or {})
    now = context.pop("now", None)
    try:
        engine = runtime.engine_payload(call, now=now, context=context or None, partial=True)
    except SemanticLayerError:
        # A draft execution can't read proves no period complete: the check reads it as one
        # with no window, which runs to now.
        engine = {"select": call.get("select"), "policy_context": {"now": now}}
    return incomplete_period_why(runtime._config, engine, call=call, start=start)


def _start_dropped_why(start: Any) -> dict[str, Any] | None:
    """Explain a window whose start a lookback metric couldn't take.

    A period comparison whose kept rows would still hold an incomplete period gets
    ``PERIOD_COMPARISON_INCOMPLETE`` instead (``period_completeness``), never this hint to execute.
    """

    if not start:
        return None
    return {
        "code": "TIME_WINDOW_START_DROPPED",
        "message": (
            f"The question's window starts {start}, but this metric looks back over earlier "
            "periods, so the query can't bound time.start. best.query returns every period "
            f"up to time.end; keep only the rows from {start} on."
        ),
        "details": {"path": "time.start", "requested_start": start},
        "recovery_hints": [
            {
                "kind": "filter_rows_after_execution",
                "message": f"Execute best.query and keep the rows dated {start} or later.",
            }
        ],
    }


def _unresolved_time_why(
    intent: str,
    partial_query: Call | None,
    consumed: tuple[tuple[int, int], ...] = (),
) -> dict[str, Any] | None:
    """Return a ``why`` envelope when the intent's time scope wasn't resolved.

    Triggers when the intent contains a time phrase the window resolver
    didn't turn into bounds, whatever window the draft happens to carry:
    only an explicit window in the caller's ``partial_query`` settles it,
    or, for an as-of phrase, a balance draft reading the day it names
    (``consumed``, the spans ``snapshot.snapshot_read`` returns).
    The shape mirrors the pattern ``blocked_reason`` envelope ({code,
    message, details, recovery_hints}).
    """

    from .time_phrases import _FISCAL_RE  # noqa: WPS433
    from .time_windows import _MAX_TIME_TEXT  # noqa: WPS433

    window = _time_window(intent)
    read = {intent.lower()[low:high].strip() for low, high in consumed}
    phrases = [phrase for phrase in window.unresolved if phrase not in read]
    too_long = len(intent) > _MAX_TIME_TEXT
    if not phrases and not too_long:
        return None
    caller_time = partial_query.time if partial_query is not None else None
    # An unread suffix may supply either missing endpoint. Only a complete caller window can
    # settle an overlong question's scope.
    if (
        caller_time is not None
        and not window.as_of
        and (caller_time.named or (not too_long and (caller_time.start or caller_time.end)))
    ):
        return None
    hour_hint = (
        [
            {
                "kind": "state_hour_range",
                "message": (
                    "State the window yourself in the plan tool's query argument: set "
                    "query.time.start and query.time.end as ISO timestamps (end-exclusive, in "
                    "the temporal role's time zone; the role must be a timestamp), with the "
                    "selected temporal_role and grain."
                ),
            }
        ]
        if window.sub_day and not too_long
        else []
    )
    return {
        "code": "TIME_WINDOW_UNRESOLVED",
        "message": (
            f"The question exceeds the {_MAX_TIME_TEXT}-character time-resolution limit; "
            "its complete time scope could not be checked."
            if too_long
            else "The question names a window shorter than a day "
            f"({'; '.join(window.sub_day)}). plan resolves days and coarser windows only, so "
            "it returns no query: one over all time would answer a different question."
            if window.sub_day
            else "The question states windows that differ from one another "
            f"({'; '.join(window.conflicts)}), so plan returns no query: picking one would "
            "answer a different question."
            if window.conflicts
            else "Which reading do you mean: " + " or ".join(window.readings) + "?"
            if window.readings
            else "The intent names a time window the planner could not resolve, so "
            "plan returns no query: one without that window would answer a "
            "different question."
        ),
        "details": {
            "path": "time",
            "unresolved_phrases": list(phrases),
            **({"conflicting_phrases": list(window.conflicts)} if window.conflicts else {}),
            **({"sub_day_phrases": list(window.sub_day)} if window.sub_day else {}),
            **({"possible_readings": list(window.readings)} if window.readings else {}),
            **({"max_intent_chars": _MAX_TIME_TEXT} if too_long else {}),
        },
        "recovery_hints": [
            *hour_hint,
            {
                "kind": "rephrase_time_window",
                "message": (
                    f"Shorten the question to at most {_MAX_TIME_TEXT} characters."
                    if too_long
                    else "A fiscal question's window resolves only from exact days: name the "
                    "period's first and last day and its fiscal bucket (e.g. 'by fiscal year "
                    "from 2017-02-01 to 2018-01-31')."
                    if _FISCAL_RE.search(intent.lower())
                    else "Rephrase the window using a supported form: "
                    + "; ".join(_SUPPORTED_WINDOW_FORMS)
                    + "."
                ),
            },
            {
                "kind": "provide_explicit_bounds",
                "message": (
                    "Or pass a complete window in the plan tool's query argument: "
                    "set query.time.start and query.time.end (end-exclusive), or "
                    "query.time.last with unit and value. Include the selected "
                    "temporal_role and grain in query.time; inspect the measure to choose them."
                ),
            },
        ],
    }


def _query_contains_conversion(runtime: Any, query: Call) -> bool:
    """True when any expression in the draft is conversion-shaped.

    Covers both ad-hoc ``kind: conversion`` IR and references to curated
    metrics whose authored expression is a conversion.
    """

    from ..expressions import ConversionExpr  # noqa: WPS433

    conversion_metric_ids = {
        str(recipe.id)
        for recipe in getattr(runtime._config, "metric_recipes", []) or []
        if isinstance(recipe.expression, ConversionExpr)
    }

    def _walk(node: Any) -> bool:
        if isinstance(node, str):
            return node in conversion_metric_ids
        if isinstance(node, dict):
            if str(node.get("kind", "")) == "conversion":
                return True
            return any(_walk(value) for value in node.values())
        if isinstance(node, (list, tuple)):
            return any(_walk(item) for item in node)
        return False

    return any(_walk(item.expression) for item in query.select)


# "converted to euros" is currency talk, not funnel talk — currencies
# are a closed-enough set to carve out.
_CURRENCY_WORDS = r"(?:euros?|eur|dollars?|usd|pounds?|gbp|yen|jpy|cad|aud|chf|local\s+currency)"
_CONVERSION_INTENT_RE_PARTS = (
    r"\bconversion\b",
    rf"\bconverts?\b(?!\s+(?:to|into)\s+{_CURRENCY_WORDS})",
    rf"\bconverted\b(?!\s+(?:to|into)\s+{_CURRENCY_WORDS})",
    r"\bfunnel\b",
    r"\b(?:and|who|customers?)\s+then\s+(?:order|orders|ordered|buy|bought|purchase[ds]?"
    r"|place[ds]?|sign(?:ed|s)?(?:\s+up)?|send[s]?|sent|return(?:ed|s)?)\b",
    r"\bfollowed\s+by\b",
)


def _conversion_intent_markers(intent: str) -> list[str]:
    import re  # noqa: WPS433

    lowered = str(intent or "").lower()
    out: list[str] = []
    for part in _CONVERSION_INTENT_RE_PARTS:
        match = re.search(part, lowered)
        if match:
            phrase = match.group(0).strip()
            if phrase not in out:
                out.append(phrase)
    return out


def _custom_conversion_operands_required(intent: str) -> bool:
    """True when the intent asks for a named-event conversion, not a curated funnel."""

    import re  # noqa: WPS433

    lowered = str(intent or "").lower()
    return bool(
        re.search(
            r"\b(?:who|customers?)\s+ordered\b.+\bthen\s+ordered\b",
            lowered,
        )
    )


def _conversion_intent_why(runtime: Any, intent: str, query: Call) -> dict[str, Any] | None:
    """Return a ``why`` envelope when a conversion ask got a non-conversion draft.

    External-agent feedback: the planner confidently returned AOV for
    "conversion rate of customers who ordered A and then ordered B
    within 28 days". The draft validated, so nothing downstream caught
    that it answers a different question. This gate keeps the draft
    available but refuses to call it ready.
    """

    from ..expressions import ConversionExpr  # noqa: WPS433

    markers = _conversion_intent_markers(intent)
    if not markers:
        return None
    query_has_conversion = _query_contains_conversion(runtime, query)
    custom_operands_required = _custom_conversion_operands_required(intent)
    if query_has_conversion and not custom_operands_required:
        return None
    conversion_metrics = [
        {"id": str(recipe.id), "label": str(getattr(recipe, "label", "") or "")}
        for recipe in getattr(runtime._config, "metric_recipes", []) or []
        if isinstance(recipe.expression, ConversionExpr)
    ]
    hints: list[dict[str, Any]] = [
        {
            "kind": "author_conversion_ir",
            "message": (
                "Author the conversion directly: an expression with kind "
                "'conversion', an 'entity' to match base and converted events "
                "on, a 'window' ({unit, value}), and 'base'/'converted' "
                "aggregate operands. Operand-level 'filter' clauses (e.g. "
                "restricting each side to a product) are applied in SQL."
            ),
        },
        {
            "kind": "discover_conversion_metrics",
            "message": "Call discover with terms like 'conversion rate funnel' to rank curated conversion metrics.",
        },
    ]
    if conversion_metrics:
        hints.insert(
            0,
            {
                "kind": "use_curated_conversion_metric",
                "message": "Curated conversion metrics exist in this package; inspect one and adapt it.",
                "conversion_metrics": conversion_metrics[:5],
            },
        )
    return {
        "code": "CONVERSION_INTENT_UNREALIZED",
        "message": (
            "The intent reads as a conversion/funnel question, but best.query "
            "does not encode the requested conversion operands — executing it as-is "
            "would answer a different question."
        ),
        "details": {
            "path": "select",
            "conversion_markers": markers,
            "query_contains_conversion": query_has_conversion,
            "custom_operands_required": custom_operands_required,
            "curated_conversion_metrics": [row["id"] for row in conversion_metrics],
        },
        "recovery_hints": hints,
    }
