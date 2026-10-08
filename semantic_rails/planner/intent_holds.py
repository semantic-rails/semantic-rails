"""Why a draft is held for time, currency or value words it doesn't carry."""

from __future__ import annotations

import re
from typing import Any

from ..ast import _time_spec_from_payload
from ..errors import SemanticLayerError
from ._base import _runtime_composition_terms
from .groupings import _requested_grouping_terms
from .intent_ir import IntentIR
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
    "explicit time.start / time.end ISO dates via partial_query",
)


def _pattern_dropped_start(
    intent: str, query: dict[str, Any], partial_query: dict[str, Any] | None
) -> str:
    """The question's window start a draft left out while keeping its end.

    The lookback retry records the start it drops; a pattern that bounds a
    period comparison drops it itself. Either way plan says so.
    """

    from .time_windows import _time_bounds_from_text  # noqa: WPS433

    caller_time = (partial_query or {}).get("time")
    if isinstance(caller_time, dict) and any(
        caller_time.get(key) for key in ("start", "end", "range")
    ):
        return ""
    expected = _time_bounds_from_text(intent)
    raw_time = query.get("time")
    time: dict[str, Any] = raw_time if isinstance(raw_time, dict) else {}
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
                    "Add the filter or limit to best.query_ir, or (plan resolves days and "
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
                    "Find what these words name with discover, add it to best.query_ir (a "
                    "group_by for a grouping, the select for a measure), then validate; or ask "
                    "the user what they mean. They name catalog objects, so asking again "
                    "without them changes the question."
                ),
            }
        ],
    }


def _qualifying_entity_why(
    runtime: Any, intent_ir: IntentIR, query: dict[str, Any]
) -> dict[str, Any] | None:
    """Parsed qualifications remain held until their cohort and scope are proven."""
    # Entity discovery also fills this slot for ordinary entity mentions.
    # A cohort obligation needs a parsed qualification, not just that hint.
    if intent_ir.qualifying_entity is None or not (
        intent_ir.qualification_phrase or intent_ir.threshold is not None
    ):
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
                    "best.query_ir, then validate; or ask the user what these words mean. "
                    "Plan can't tell whether they change the question, so asking again "
                    "without them may answer another one."
                ),
            }
        ],
    }


def _time_assumptions(intent: str, query: dict[str, Any]) -> list[str]:
    """The reading plan took of an open end in the window the draft carries."""

    window = _time_window(intent)
    time = query.get("time")
    if not window.assumptions or not isinstance(time, dict):
        return []
    if any(time.get(key) != window.bounds.get(key) for key in ("start", "end")):
        return []
    return list(window.assumptions)


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
            "periods, so the query can't bound time.start. best.query_ir returns every period "
            f"up to time.end; keep only the rows from {start} on."
        ),
        "details": {"path": "time.start", "requested_start": start},
        "recovery_hints": [
            {
                "kind": "filter_rows_after_execution",
                "message": f"Execute best.query_ir and keep the rows dated {start} or later.",
            }
        ],
    }


def _with_query_clock(
    config: Any, query: dict[str, Any], partial_query: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The query with a relative window as the dates the caller's clock gives it.

    Query IR carries no ``policy_context`` (the merge drops it), so a returned ``time.range``
    would run on the executing caller's clock. With ``policy_context.now`` supplied, the range
    becomes the bounds execution computes from that clock in the role's zone. None when they
    can't be computed.
    """

    context = (partial_query or {}).get("policy_context")
    time = query.get("time")
    if not (
        isinstance(context, dict)
        and context.get("now") not in (None, "")
        and isinstance(time, dict)
        and time.get("range")
    ):
        return query
    try:
        spec = _time_spec_from_payload(time, policy_context=context, config=config)
    except (SemanticLayerError, ValueError, OverflowError):
        return None
    if spec is None or not spec.start or not spec.end:
        return None
    bounded = {key: value for key, value in time.items() if key != "range"}
    return {**query, "time": {**bounded, "start": spec.start, "end": spec.end}}


def _unclocked_window_why(
    config: Any, intent: str, query: dict[str, Any], partial_query: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Hold a draft whose relative window can't take the supplied clock."""

    if _with_query_clock(config, query, partial_query) is not None:
        return None
    lowered = intent.lower()
    return {
        "code": "TIME_WINDOW_UNRESOLVED",
        "message": (
            "The draft's relative window could not be bounded on policy_context.now, so plan "
            "returns no query: run on another clock it would answer a different question."
        ),
        "details": {
            "path": "time.range",
            "unresolved_phrases": [
                lowered[low:high].strip() for (low, high), _bounds in _time_window(intent).windows
            ],
        },
        "recovery_hints": [
            {
                "kind": "provide_explicit_bounds",
                "message": (
                    "Pass query.time.start and query.time.end (end-exclusive) in the plan "
                    "tool's query argument instead of a relative window."
                ),
            }
        ],
    }


def _unresolved_time_why(
    intent: str,
    partial_query: dict[str, Any] | None,
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

    window = _time_window(intent, policy_context=(partial_query or {}).get("policy_context"))
    read = {intent.lower()[low:high].strip() for low, high in consumed}
    phrases = [phrase for phrase in window.unresolved if phrase not in read]
    too_long = len(intent) > _MAX_TIME_TEXT
    if not phrases and not too_long:
        return None
    caller_time = (partial_query or {}).get("time")
    if isinstance(caller_time, dict) and not window.as_of:
        # An unread suffix may supply either missing endpoint. Only a
        # complete caller window can settle an overlong question's scope.
        complete = caller_time.get("range") or (caller_time.get("start") and caller_time.get("end"))
        if complete or (not too_long and any(caller_time.get(key) for key in ("start", "end"))):
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
            else "The intent names a time window the planner could not resolve, so "
            "plan returns no query: one without that window would answer a "
            "different question."
        ),
        "details": {
            "path": "time",
            "unresolved_phrases": list(phrases),
            **({"conflicting_phrases": list(window.conflicts)} if window.conflicts else {}),
            **({"sub_day_phrases": list(window.sub_day)} if window.sub_day else {}),
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
                    "query.time.range.last with unit and value. Include the selected "
                    "temporal_role and grain in query.time; inspect the measure to choose them."
                ),
            },
        ],
    }


def _query_contains_conversion(runtime: Any, query: dict[str, Any]) -> bool:
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
        if isinstance(node, dict):
            if str(node.get("kind", "")) == "conversion":
                return True
            metric_ref = str(node.get("metric", "") or node.get("metric_recipe", "") or "")
            if metric_ref and metric_ref in conversion_metric_ids:
                return True
            return any(_walk(value) for value in node.values())
        if isinstance(node, list):
            return any(_walk(item) for item in node)
        return False

    return _walk(query if isinstance(query, dict) else {})


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


def _conversion_intent_why(
    runtime: Any, intent: str, query: dict[str, Any]
) -> dict[str, Any] | None:
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
            "The intent reads as a conversion/funnel question, but best.query_ir "
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
