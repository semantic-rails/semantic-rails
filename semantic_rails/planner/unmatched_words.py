from __future__ import annotations

import re
from collections import Counter
from typing import Any

from ._base import _NUMBER_WORDS, _TERM_SYNONYMS, _name_matches, _singular, _tokens
from .consumed_spans import _TERM_RE, _ZONE_NAME_RE, _consumed_spans
from .coverage import (
    _PRIOR_PERIOD_RE,
    _catalog_rows,
    _core_text,
    _dict_nodes,
    _plain,
    _query_contains_prior_period,
    _referenced_ids,
    _time_block,
    _value_names,
    _value_phrases,
)
from .filter_checks import _excluded_value_spans, _field_predicates, _positive_filter_evidence
from .groupings import _explicit_grain, _requested_grouping_spans
from .time_checks import _fiscal_calendar_gaps
from .time_phrases import (
    _FISCAL_RE,
    _MONTH_NUMBERS,
    _ORDINALS,
    _PERIOD_SHIFT_TRIGGERS,
    _TIME_UNITS,
    _names_time_axis,
)
from .time_windows import _time_window

# Words that frame a question rather than constrain it: question and request
# words, ranking, comparison and calendar vocabulary, and generic aggregation
# words. Time phrases the planner reads, counts and ordinals are skipped
# separately.
_FRAMING_WORDS = frozenset(
    {
        *[
            "all",
            "amount",
            "be",
            "been",
            "break",
            "breakdown",
            "calculate",
            "can",
            "come",
            "comes",
            "compare",
            "compute",
            "could",
            "display",
            "down",
            "each",
            "every",
            "find",
            "give",
            "group",
            "grouped",
            "know",
            "let",
            "lets",
            "level",
            "levels",
            "like",
            "list",
            "look",
            "me",
            "need",
            "number",
            "numbers",
            "please",
            "report",
            "see",
            "split",
            "sum",
            "total",
            "totals",
            "trend",
            "trending",
            "trends",
            "overall",
            "view",
            "volume",
            "want",
            "whose",
            # Verbs that restate a measure ("tax collected", "customers who spent").
            "brought",
            "collect",
            "collected",
            "earned",
            "generated",
            "had",
            "made",
            "sell",
            "sells",
            "sold",
            "spent",
            # Verbs and function words that restate the request ("orders dated in March",
            # "customers who placed", "counted using").
            "anchored",
            "came",
            "counted",
            "dated",
            "only",
            "placed",
            "such",
            "using",
            "while",
            # Comparison and combination words; the select list carries them.
            "across",
            "against",
            "alongside",
            "combined",
            "compared",
            "comparison",
            "together",
            # Negations; the negation check owns them.
            "except",
            "excluding",
            "not",
            "without",
        ],
        *[
            "top",
            "bottom",
            "highest",
            "lowest",
            "most",
            "least",
            "fewest",
            "largest",
            "smallest",
            "biggest",
            "best",
            "worst",
            "greatest",
            "rank",
            "ranked",
            "ranking",
            "selling",
            "performing",
        ],
        *[
            "day",
            "days",
            "week",
            "weeks",
            "month",
            "months",
            "quarter",
            "quarters",
            "year",
            "years",
            "daily",
            "weekly",
            "monthly",
            "quarterly",
            "yearly",
            "annual",
            "annually",
            "half",
            "h1",
            "h2",
            "q1",
            "q2",
            "q3",
            "q4",
            "date",
            "dates",
            "time",
            "period",
            "periods",
            "through",
            "until",
            "during",
            "ever",
            "first",
            "second",
            "last",
        ],
        *_MONTH_NUMBERS,
    }
)
_ORDINAL_RE = re.compile(r"\d+(?:st|nd|rd|th)")
# The most words one PLAN_UNMATCHED_TERMS warning names, and the most distinct
# question words read to find them.
_MAX_UNMATCHED_TERMS = 8
_MAX_SCANNED_WORDS = 256


def _unmatched_words(runtime: Any, question: str, query: dict[str, Any]) -> list[str]:
    """Question words the draft accounts for nowhere (the warning; readiness is
    ``unconsumed_terms`` and ``unconsumed_catalog_words``)."""

    from ..metadata_parts.relevance import _INTENT_STOPWORDS  # noqa: WPS433

    referenced = _used_ids(runtime._config, query)
    calendar_id = str(_time_block(query).get("calendar_id") or "default")
    vocabulary: set[str] = set()
    for row in _catalog_rows(runtime._config):
        if str(getattr(row, "id", "")) in referenced or (
            calendar_id != "default" and getattr(row, "calendar_id", "") == calendar_id
        ):
            # Names only: a description that says "not discounts" doesn't answer "discounts".
            vocabulary.update(_tokens(_core_text(row)))
    labels = _value_phrases(runtime._config)
    for node in _dict_nodes(query):
        if "field" in node and "value" in node:
            value = node.get("value")
            for item in value if isinstance(value, list) else [value]:
                vocabulary.update(_tokens(str(item)))
                # A filter on 'jaffle' accounts for the question's "food".
                for _domain, row in labels.get(_plain(item), []):
                    vocabulary.update(_tokens(" ".join(_value_names(row))))
    by_initial: dict[str, list[str]] = {}
    for known in vocabulary:
        by_initial.setdefault(known[:1], []).append(known)
    skipped = _INTENT_STOPWORDS | _FRAMING_WORDS | set(_NUMBER_WORDS) | set(_ORDINALS)
    text = str(question or "")
    lowered = text.lower()
    time_spans = [*_time_window(text).spans, *_honored_clause_spans(runtime, text, query)]
    tokens = [(match.group(0), *match.span()) for match in _TERM_RE.finditer(lowered)]
    consumed = _consumed_spans(runtime, lowered, tokens, query)

    def in_time(start: int, end: int) -> bool:
        return any(start < span_end and span_start < end for span_start, span_end in time_spans)

    scanned: set[str] = set()
    reported: set[str] = set()
    out: list[str] = []
    for word, start, end in tokens:
        scanned.add(word)
        if len(scanned) > _MAX_SCANNED_WORDS:
            break
        if word in reported:
            continue
        numeral = any(char.isdigit() for char in word)
        plain = re.fullmatch(r"\d+(?:[.,]\d+)*", word) is not None
        token = _TERM_SYNONYMS.get(word, word)
        if (
            (len(word) < 2 and not numeral)
            # A number counts, so a construct of the draft must read it where the question
            # states it: a "2 or more" the draft dropped is named.
            or (plain and any(low <= start and end <= high for low, high in consumed))
            or _ORDINAL_RE.fullmatch(word)
            or word in skipped
            or token in skipped
            or token in vocabulary
            or _singular(token) in vocabulary
            or in_time(start, end)
            or (not numeral and _one_typo_away(token, by_initial))
        ):
            continue
        reported.add(word)
        out.append(word)
    for zone_name in _ZONE_NAME_RE.finditer(text):
        # "Europe/Berlin" reads as two plain words, so it is named whole.
        name = zone_name.group(0).lower()
        if name not in reported and not in_time(*zone_name.span()):
            reported.add(name)
            out.append(name)
    return out


def _used_ids(config: Any, query: dict[str, Any]) -> set[str]:
    """The objects a draft uses: those it names, and the entity and clock of each measure or
    metric it names ("revenue from orders" uses the Order entity of Revenue)."""

    used = set(_referenced_ids(query))
    for row in [*config.measures, *config.metric_recipes]:
        if str(row.id) in used:
            for attr in ("entity", "default_temporal_role", "temporal_role"):
                value = getattr(row, attr, None)
                if isinstance(value, str) and value:
                    used.add(value)
    return used


def _honored_clause_spans(runtime: Any, text: str, query: dict[str, Any]) -> list[tuple[int, int]]:
    """The clauses another check owns, when the draft honors them: a fiscal calendar ("fiscal
    revenue on April 3, 2017"), a prior-period comparison, or an included/excluded value."""

    lowered = text.lower()
    spans: list[tuple[int, int]] = []
    if not _fiscal_calendar_gaps(runtime._config, text, query):
        spans.extend(match.span() for match in _FISCAL_RE.finditer(lowered))
    if _query_contains_prior_period(runtime, query):
        spans.extend(match.span() for match in _PRIOR_PERIOD_RE.finditer(lowered))
    for marker in re.finditer(r"\b(?:including|include)\s+", lowered):
        negative = any(start <= marker.start() < end for start, end in _excluded_value_spans(text))
        predicates = _field_predicates(query)
        for phrase, rows in _value_phrases(runtime._config).items():
            pattern = re.escape(phrase).replace(r"\ ", r"[\s_-]+") + r"\b"
            value = re.match(pattern, lowered[marker.end() :])
            if value is None:
                continue
            honored = (
                any(
                    predicates[dimension].drops(row.value)
                    for domain, row in rows
                    for dimension in domain.dimensions
                    if dimension in predicates
                )
                if negative
                else bool(_positive_filter_evidence(runtime, query, phrase))
            )
            if honored:
                spans.append((marker.start(), marker.end() + value.end()))
    return spans


def unmatched_intent_terms(runtime: Any, question: str, query: dict[str, Any]) -> list[str]:
    """Question words the draft accounts for nowhere, in question order.

    A word is accounted for when it frames the question, sits in a time phrase
    the planner read, counts or orders ("five", "3rd"), or appears (allowing a
    plural or one typo) in the id, name, label or aliases of an object the
    draft uses or in one of its filter values. A description never accounts
    for a word. Words come back as the question spells them, at most eight.
    """

    return _unmatched_words(runtime, question, query)[:_MAX_UNMATCHED_TERMS]


def unconsumed_catalog_words(runtime: Any, question: str, query: dict[str, Any]) -> list[str]:
    """The question's words that name a catalog object (``_own_words``; a plural counts as its
    singular) and that the draft doesn't consume: the readiness invariant for words, beside
    ``unconsumed_terms`` for numbers.

    Only the draft consumes one: by the own words of an object it selects, a value it filters on,
    a time grain or count it carries, or a time phrase or clause it honors; function words
    are exempt only when they aren't exact catalog names. A synonym, a typo, a namespace,
    a description, a framing word or an object it doesn't select never does, so one catalog
    name can't stand in for another. One left over is a dropped grouping or a swapped subject.
    Every word is read.
    """

    return _unconsumed_words(runtime, question, query)[0]


def unconsumed_unknown_words(runtime: Any, question: str, query: dict[str, Any]) -> list[str]:
    """Unconsumed words that name no catalog object, in question order."""

    return _unconsumed_words(runtime, question, query)[1]


def _unconsumed_words(
    runtime: Any, question: str, query: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """One consumption pass classifies leftover catalog names and unknown words."""

    from ..metadata_parts.relevance import _INTENT_STOPWORDS  # noqa: WPS433

    text = str(question or "")
    lowered = text.lower()
    spans = [*_time_window(text).spans, *_honored_clause_spans(runtime, text, query)]
    referenced = set(_referenced_ids(query))
    selected = {"expressions": [item.get("expression") for item in query.get("select", [])]}
    selected_ids = set(_referenced_ids(selected))
    measures = {row.id: row for row in runtime._config.measures}
    distinct_words = {
        word
        for node in _dict_nodes(query)
        if (row := measures.get(node.get("measure"))) is not None
        and node.get("aggregation", row.default_aggregation) == "count_distinct"
        for word in _own_words(row)
    }
    for match in re.finditer(r"\bdistinct\s+([^\W_]+)\b", lowered):
        if _singular(match.group(1)) in distinct_words:
            spans.append(match.span())
    count_valued = any(
        row.id in selected_ids
        and (
            row.default_aggregation in ("count", "count_distinct")
            or row.value_type == "count"
            or "count" in _own_words(row)
        )
        for row in runtime._config.measures
    ) or any(
        node.get("aggregation") in ("count", "count_distinct") for node in _dict_nodes(selected)
    )
    calendar_id = str(_time_block(query).get("calendar_id") or "default")
    time = _time_block(query)
    clock = next(
        (
            row.label
            for row in runtime._config.temporal_roles
            if row.id == time.get("temporal_role")
        ),
        "",
    )
    clock_units: Counter[str] = Counter()
    for match in re.finditer(r"\bat\s+(day|week|month|quarter|year)\s+grain\b", lowered):
        if match.group(1) == time.get("grain"):
            spans.append(match.span())
    if clock and time.get("grain") == _explicit_grain(text, clock):
        clock_spans = [
            (start, end)
            for start, end in _requested_grouping_spans(text)
            if _names_time_axis(lowered[start:end], clock)
        ]
        # The time block carries one clock grouping. With a second ("by order month and order
        # date"), the draft drops one of them, so neither is consumed.
        if len(clock_spans) == 1:
            [(start, end)] = clock_spans
            units = [_singular(match.group(0)) for match in _TERM_RE.finditer(lowered[start:end])]
            units = [unit for unit in units if unit in _TIME_UNITS]
            if all(unit == time.get("grain") for unit in units):
                spans.append((start, end))
                clock_units.update(units)
    names: set[str] = set()
    exact_names: set[str] = set()
    used: set[str] = set()
    for row in _catalog_rows(runtime._config):
        own = _own_words(row)
        names |= own
        # "Show" names an object; "of" inside "Share of revenue" doesn't name one.
        exact_names.update(
            _plain(str(getattr(row, attr, "") or "").rpartition(".")[2]) for attr in ("id", "name")
        )
        exact_names.update(
            _plain(str(name))
            for name in [getattr(row, "label", "") or "", *(getattr(row, "aliases", None) or [])]
        )
        if str(getattr(row, "id", "")) in referenced or (
            calendar_id != "default" and getattr(row, "calendar_id", "") == calendar_id
        ):
            # Multi-word synonyms consume only their contiguous spans.
            used |= _own_words(row, phrase_words=False)
            spans.extend((start, end) for _, start, end in _name_matches(row, lowered))
            # The question spelling its whole id or name ("metric.sales.aov_usd") uses that span.
            for attr in ("id", "name"):
                path = re.escape(str(getattr(row, attr, "") or "").lower())
                found = re.finditer(rf"(?<![\w.]){path}(?!\w|\.\w)", lowered) if path else ()
                spans.extend(match.span() for match in found)
    labels = _value_phrases(runtime._config)
    reads: Counter[str] = Counter()
    for node in _dict_nodes(query):
        if "field" in node and "value" in node:
            value = node["value"]
            for item in value if isinstance(value, list) else [value]:
                used.update(_plain(item).split())
                for domain, row in labels.get(_plain(item), []):
                    if str(node["field"]) in domain.dimensions:
                        used.update(_plain(" ".join(_value_names(row))).split())
        for grain in (node.get("grain"), node.get("time_grain")):
            if grain not in _TIME_UNITS:
                continue
            # A prior period's grain shifts the clock; it never reads a grouping outside
            # its trigger span. A grouping grain reads only its own unit, once.
            if node.get("kind") == "prior_period":
                for pattern, unit in _PERIOD_SHIFT_TRIGGERS:
                    found = re.finditer(pattern, lowered) if unit == grain else ()
                    spans.extend(match.span() for match in found)
            else:
                reads.update({grain, "daily" if grain == "day" else f"{grain}ly"})
    reads.subtract(clock_units)
    if count_valued and not query.get("group_by"):
        # Entity counts normalize to count_distinct; snapshot counts can use last_value.
        # These read "number of" only when the draft has no group_by. A clock grain
        # can still carry a monthly count without grouping by a catalog dimension.
        spans.extend(match.span() for match in re.finditer(r"\bnumber\s+of\b", lowered))
    named = names | {_singular(word) for word in names}
    consumed = used | {_singular(word) for word in used}
    skipped = _INTENT_STOPWORDS | set(_NUMBER_WORDS)
    unknown_skipped = (
        skipped | _FRAMING_WORDS | {"make", "made", "earn", "earned", "generate", "generated"}
    )
    out: list[str] = []
    unknown: list[str] = []
    for match in _TERM_RE.finditer(lowered):
        word, (start, end), key = match.group(0), match.span(), _singular(match.group(0))
        forms = {word, key}
        # Plain -s and -ies use _singular. Only s/x/z/ch/sh take -es, with no invented
        # two-letter stem ("uses" isn't the catalog name "us"; "ones" isn't "on").
        stem = word.removesuffix("es")
        if word.endswith("es") and len(stem) > 2 and stem.endswith(("s", "x", "z", "ch", "sh")):
            forms.add(stem)
        if (
            forms & consumed
            or (word in skipped and word not in exact_names)
            # A number is unconsumed_terms' to check, by where the draft reads it.
            or any(char.isdigit() for char in word)
            or any(low < end and start < high for low, high in spans)
        ):
            continue
        if reads[key] > 0:
            reads[key] -= 1
        elif not forms & named and word in unknown_skipped:
            continue
        else:
            # Classify only after every regular plural form is known.
            remaining = out if forms & named else unknown
            if word not in remaining:
                remaining.append(word)
    return out, unknown


def _own_words(row: Any, *, phrase_words: bool = True) -> set[str]:
    """The words that name an object, as written: those of its label and aliases, and those of
    the last dotted part of its id and name that aren't one of its own namespaces ("sales" in
    "metric.sales.aov_usd", which is named "jaffle.sales_aov_usd")."""

    paths = [str(getattr(row, attr, "") or "") for attr in ("id", "name")]
    spaces = set(_plain(" ".join(path.rpartition(".")[0] for path in paths)).split())
    leaves = set(_plain(" ".join(path.rpartition(".")[2] for path in paths)).split())
    aliases = [
        alias
        for alias in (getattr(row, "aliases", None) or [])
        if phrase_words or len(_TERM_RE.findall(str(alias))) == 1
    ]
    declared = [getattr(row, "label", "") or "", *aliases]
    return (leaves - spaces) | set(_plain(" ".join(map(str, declared))).split())


def _one_typo_away(word: str, by_initial: dict[str, list[str]]) -> bool:
    """A misspelling of a known word: same first letter and one edit.

    A four-letter word only counts when it drops a letter from a longer one
    with the same first two ("stor" for "store"), so "next" isn't "net".
    """

    if len(word) < 4:
        return False
    for known in by_initial.get(word[0], ()):
        if len(word) == 4 and (len(known) != 5 or known[:2] != word[:2]):
            continue
        if abs(len(known) - len(word)) <= 1 and _within_one_edit(word, known):
            return True
    return False


def _within_one_edit(left: str, right: str) -> bool:
    """One insertion, deletion, substitution or swap of adjacent letters."""

    if left == right:
        return True
    if len(left) == len(right):
        diffs = [index for index, (a, b) in enumerate(zip(left, right, strict=True)) if a != b]
        if len(diffs) == 1:
            return True
        return (
            len(diffs) == 2
            and diffs[1] == diffs[0] + 1
            and left[diffs[0]] == right[diffs[1]]
            and left[diffs[1]] == right[diffs[0]]
        )
    shorter, longer = sorted((left, right), key=len)
    if len(longer) - len(shorter) != 1:
        return False
    index = 0
    while index < len(shorter) and shorter[index] == longer[index]:
        index += 1
    return shorter[index:] == longer[index + 1 :]
