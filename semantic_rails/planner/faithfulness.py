"""Conservative intent-to-Query-IR faithfulness diagnostics.

Validation proves that a Query IR is executable; it does not prove that the IR
answers every clause in the user's question.  This module checks a deliberately
small set of high-confidence structural cues whose realization is observable in
Query IR.  A detected gap downgrades a validating plan instead of guessing how
to repair it.

The checks are compositional rather than pattern-specific.  A newly added
planner pattern automatically passes once its Query IR contains the requested
structure.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ._base import (
    _MONTH_NUMBERS,
    _NUMBER_WORDS,
    _ORDINALS,
    _TERM_SYNONYMS,
    _TIME_UNITS,
    _canonical_measure,
    _canonical_metric,
    _named_metric,
    _object_text,
    _tied_top,
    _time_bounds_from_text,
    _time_window,
    _tokens,
)
from .generators import _target_focus_text
from .intent_ir import IntentIR


@dataclass(frozen=True)
class CoverageGap:
    """One requested clause not faithfully represented in Query IR."""

    kind: str
    clause: str
    message: str
    expected: dict[str, Any] = field(default_factory=dict)
    actual: dict[str, Any] = field(default_factory=dict)
    recovery_hint: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "clause": self.clause,
            "message": self.message,
        }
        if self.expected:
            out["expected"] = self.expected
        if self.actual:
            out["actual"] = self.actual
        return out


_PARTITIONED_RANK_RE = re.compile(
    r"\b(?:within|inside)\s+(?:each|every)\b|"
    r"\b(?:in|for)\s+(?:each|every)\b",
    re.IGNORECASE,
)
_RANK_RE = re.compile(
    r"\b(?:top|bottom)\s+\d+\b|\brank(?:ed|ing)?\b|"
    r"\b\d+\s+(?:highest|lowest|best|worst)\b",
    re.IGNORECASE,
)
_PRIOR_PERIOD_RE = re.compile(
    r"\b(?:compared\s+(?:with|to)|vs\.?|versus|against|alongside|along\s+with|next\s+to)\s+"
    r"(?:the\s+)?(?:last|prior|previous)\s+(?:day|week|month|quarter|year|period)\b",
    re.IGNORECASE,
)
_EXCLUSION_VALUE_RE = (
    r"(?P<value>[^,.;]+?)(?=\s+(?:and\s+)?(?:excluding|except|without|but\s+not|not)\b|[,.;]|$)"
)
_NEGATION_RE = re.compile(
    r"\b(?P<marker>excluding|except|without|but\s+not|not)\s+"
    r"(?!only\b)" + _EXCLUSION_VALUE_RE,
    re.IGNORECASE,
)
# "for all stores but Brooklyn": "but" excludes after all/every/each/any.
_ALL_BUT_RE = re.compile(
    r"\b(?:all|every|each|any)\s+(?:[a-z-]+\s+){0,3}?(?P<marker>but)\s+(?!not\b)"
    + _EXCLUSION_VALUE_RE,
    re.IGNORECASE,
)
# Ranking requests: "top 5 products", "the 3 lowest-selling products", "the 5
# customers who spent the most", "which store had the most orders", "rank
# stores by revenue". "At least 10 orders" is a threshold and "the 5 most
# recent months" a window; neither is a ranking.
_SUPERLATIVES = frozenset(
    {
        "best",
        "biggest",
        "fewest",
        "greatest",
        "highest",
        "largest",
        "least",
        "lowest",
        "most",
        "smallest",
        "worst",
    }
)
_ASCENDING = frozenset({"bottom", "fewest", "least", "lowest", "smallest", "worst"})
_RECENCY = frozenset({"earliest", "latest", "least-recent", "most-recent", "newest", "oldest"})
_WORD_RE = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")
_YEAR_NUMBER_RE = re.compile(r"(?:19|20)\d{2}")
# Words that end a ranked noun phrase ("which store had ...", "top 5 products by ...").
_PHRASE_BREAKS = frozenset(
    [
        "by",
        "in",
        "for",
        "with",
        "from",
        "of",
        "on",
        "at",
        "per",
        "that",
        "who",
        "which",
        "whose",
        "where",
        "when",
        "during",
        "over",
        "has",
        "had",
        "have",
        "is",
        "was",
        "were",
        "are",
        "does",
        "did",
        "do",
        "sells",
        "sold",
        "made",
        "makes",
        "drove",
        "drives",
        "generated",
        "generates",
        "brought",
        "got",
        "gets",
        "saw",
        "sees",
        "spent",
        "spends",
    ]
)
# Shares of a population: "the top decile of customers" is a threshold.
_SHARE_WORDS = frozenset(
    {
        "decile",
        "deciles",
        "fifth",
        "half",
        "pct",
        "percent",
        "percentile",
        "percentiles",
        "quartile",
        "quartiles",
        "quintile",
        "quintiles",
        "third",
        "tier",
    }
)
# Words that open the clause naming a ranking's order ("the store with the most
# orders", "3 products that sold the least").
_RELATIVE = frozenset({"having", "that", "which", "who", "whose", "with"})
# Words that can't be the thing ranked ("which one is ...", "which have ...").
_NOT_RANKED = frozenset(
    {
        *_PHRASE_BREAKS,
        *_SUPERLATIVES,
        *_NUMBER_WORDS,
        *_SHARE_WORDS,
        "a",
        "all",
        "an",
        "and",
        "any",
        "be",
        "been",
        "bottom",
        "can",
        "could",
        "each",
        "every",
        "it",
        "its",
        "me",
        "my",
        "one",
        "ones",
        "or",
        "our",
        "should",
        "some",
        "the",
        "their",
        "them",
        "these",
        "they",
        "this",
        "those",
        "to",
        "top",
        "us",
        "we",
        "what",
        "will",
        "would",
        "you",
        "your",
    }
)
_SUBJECT_CONJUNCTION_RE = re.compile(r"\s+(?:and|plus)\s+|\s*,\s*", re.IGNORECASE)
_SUBJECT_BOUNDARY_RE = re.compile(
    r"\s+(?:by|where|during|over\s+time|for\s+(?:customers?|stores?|accounts?|users?)|"
    r"with\s+(?:at\s+least|more\s+than|over|under))\b",
    re.IGNORECASE,
)
_SUBJECT_FILLER = frozenset(
    {
        "a",
        "all",
        "average",
        "avg",
        "calculate",
        "count",
        "daily",
        "give",
        "historical",
        "how",
        "is",
        "list",
        "many",
        "me",
        "monthly",
        "of",
        "please",
        "quarterly",
        "show",
        "sum",
        "the",
        "total",
        "was",
        "weekly",
        "what",
        "yearly",
    }
)
_NEGATIVE_OPS = frozenset(
    {
        "!=",
        "<>",
        "IS NOT",
        "IS NOT NULL",
        "NOT IN",
        "NOT LIKE",
        "NOT BETWEEN",
    }
)
# Filter ops that keep the values they name, and ops that drop them.
_KEEPING_OPS = frozenset({"=", "==", "IN"})
_EXCLUDING_OPS = frozenset({"!=", "<>", "NOT IN"})
# Everyday words that are also values in some catalogs ("new", "all", "other",
# "us", "open"). One names its value only next to a word of the value's
# dimension ("new customers"); otherwise the PLAN_UNMATCHED_TERMS warning,
# which never downgrades a plan, covers it.
_EVERYDAY_WORDS = frozenset(
    {
        "active",
        "all",
        "any",
        "average",
        "best",
        "big",
        "closed",
        "current",
        "first",
        "good",
        "high",
        "inactive",
        "large",
        "last",
        "less",
        "low",
        "more",
        "new",
        "next",
        "no",
        "none",
        "normal",
        "old",
        "open",
        "other",
        "others",
        "same",
        "small",
        "standard",
        "top",
        "total",
        "us",
        "yes",
    }
)
# Identifier words too generic to tie an everyday word to a dimension.
_GENERIC_ID_WORDS = frozenset(
    {"code", "dimension", "flag", "has", "id", "is", "key", "name", "status", "type", "value"}
)


def intent_faithfulness_why(
    runtime: Any,
    *,
    question: str,
    intent_ir: IntentIR,
    query: dict[str, Any],
    partial_query: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a structured downgrade reason for high-confidence coverage gaps.

    A window in the caller's ``partial_query`` settles the question's time
    window, as it does for ``TIME_WINDOW_UNRESOLVED``.
    """

    gaps: list[CoverageGap] = []

    text = str(question or "")
    named = _named_metric(runtime._config, text)
    if named is not None and str(named[0].id) in _referenced_ids(query):
        # The chosen metric answers its own name ("revenue, trailing 7 days").
        text = named[1]
    elif named is not None:
        gaps.append(
            CoverageGap(
                kind="named_metric_unrealized",
                clause=str(named[0].label),
                message="The question names a governed metric, but the draft doesn't use it.",
                expected={"metric": str(named[0].id)},
                actual={"subjects": _projected_subject_ids(query)},
                recovery_hint={
                    "kind": "use_named_metric",
                    "message": "Select the named metric in Query IR, then validate.",
                },
            )
        )
    partition_match = _PARTITIONED_RANK_RE.search(text)
    if (
        partition_match
        and (intent_ir.is_top_intent or _RANK_RE.search(text))
        and not _query_has_partitioned_ranking(query)
    ):
        gaps.append(
            CoverageGap(
                kind="partitioned_ranking_unrealized",
                clause=partition_match.group(0),
                message=(
                    "The question requests ranking within each group, but the draft only proves "
                    "a global ordering/limit."
                ),
                expected={"ranking_scope": "partitioned"},
                actual={
                    "group_by": list(query.get("group_by") or []),
                    "order_by": list(query.get("order_by") or []),
                    "limit": query.get("limit"),
                    "partition_by": [],
                },
                recovery_hint={
                    "kind": "author_partitioned_ranking",
                    "message": (
                        "Do not execute this as within-group top-N. Author an explicitly "
                        "partitioned ranking through a supported Query IR/relation shape, or "
                        "run one governed query per parent group."
                    ),
                },
            )
        )

    period_match = _PRIOR_PERIOD_RE.search(text)
    if period_match and not _query_contains_prior_period(runtime, query):
        gaps.append(
            CoverageGap(
                kind="prior_period_comparison_unrealized",
                clause=period_match.group(0),
                message=(
                    "The question requests a prior-period comparison, but no prior-period "
                    "expression or governed prior-period metric is present."
                ),
                expected={"expression_kind": "prior_period"},
                actual={"selected_expression_kinds": _selected_expression_kinds(query)},
                recovery_hint={
                    "kind": "rephrase_or_author_prior_period",
                    "message": (
                        "Use a supported phrase such as 'revenue vs prior year', or author a "
                        "second select with kind 'prior_period' and validate it."
                    ),
                },
            )
        )

    # Named-value coverage suppresses a reversal when this check reports it,
    # so every exclusion clause must be inspected, not only the first one.
    negation_matches = _exclusion_matches(text)
    excluded_spans = _excluded_value_spans(text)
    for negation_match in negation_matches:
        excluded_span = next(
            (span for span in excluded_spans if span[0] == negation_match.start("value")),
            negation_match.span("value"),
        )
        excluded_text = text[excluded_span[0] : excluded_span[1]].strip()
        positive_filters = _positive_filter_evidence(runtime, query, excluded_text)
        reversed_clause = bool(positive_filters)
        negative_present = _query_has_negative_semantics(query)
        # A matching positive predicate is still a reversal when an unrelated
        # (or even contradictory) negative predicate also happens to exist.
        if reversed_clause or not negative_present:
            gaps.append(
                CoverageGap(
                    kind=("negation_reversed" if reversed_clause else "negation_unrealized"),
                    clause=negation_match.group(0).strip(),
                    message=(
                        "The excluded value is encoded by a positive filter, reversing the request."
                        if reversed_clause
                        else "The question contains an exclusion, but the draft has no negative predicate."
                    ),
                    expected={"filter_polarity": "negative", "excluded_text": excluded_text},
                    actual={
                        "where": list(query.get("where") or []),
                        "positive_matches": positive_filters,
                        "negative_predicate_present": negative_present,
                    },
                    recovery_hint={
                        "kind": "provide_negative_filter",
                        "message": (
                            "Pass an explicit Query IR/partial_query filter using != or NOT IN for "
                            "the excluded value, then validate before execution."
                        ),
                    },
                )
            )

    requested_subjects = _conjoined_subjects(runtime, text)
    if len(requested_subjects) >= 2:
        projected = set(_projected_subject_ids(query))
        missing = [row for row in requested_subjects if projected.isdisjoint(row["candidate_ids"])]
        if missing:
            gaps.append(
                CoverageGap(
                    kind="multiple_subjects_unrealized",
                    clause=" and ".join(row["phrase"] for row in requested_subjects),
                    message=(
                        "The question clearly requests multiple governed subjects, but one or "
                        "more are absent from the top-level select list."
                    ),
                    expected={"subjects": requested_subjects},
                    actual={"projected_subject_ids": sorted(projected), "missing": missing},
                    recovery_hint={
                        "kind": "provide_multiple_selects",
                        "message": (
                            "Use 'X vs Y' for a supported side-by-side comparison, or provide "
                            "one explicit Query IR select entry per requested subject."
                        ),
                    },
                )
            )

    caller_time = (partial_query or {}).get("time")
    if not (
        isinstance(caller_time, dict)
        and any(caller_time.get(key) for key in ("start", "end", "range"))
    ):
        gaps.extend(_time_window_gaps(runtime, text, query))
    gaps.extend(_ranking_gaps(runtime, text, query))
    gaps.extend(_where_clause_gaps(runtime, text, query))
    contradictions = _contradictory_filter_gaps(query)
    if contradictions:
        # No row can satisfy the draft. Report that decisive failure once;
        # value-specific absences are consequences of the same contradiction.
        gaps.extend(contradictions)
    else:
        gaps.extend(_filter_value_gaps(runtime, text, query))

    return _coverage_why(gaps)


def _coverage_why(gaps: list[CoverageGap]) -> dict[str, Any] | None:
    if not gaps:
        return None
    return {
        "code": "PLAN_INTENT_COVERAGE_GAP",
        "message": (
            "The draft validates as Query IR, but one or more high-confidence clauses from the "
            "question are not faithfully represented. It is not ready to execute as written."
        ),
        "details": {
            "gap_count": len(gaps),
            "gaps": [gap.to_dict() for gap in gaps],
        },
        "recovery_hints": _unique_hints(gaps),
    }


def intent_subject_why(
    runtime: Any,
    *,
    question: str,
    intent_ir: IntentIR,
    query: dict[str, Any],
    partial_query: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """A coverage gap when the ranking tied the draft's one subject with others
    and neither the question nor the caller's ``partial_query`` names that
    subject (see ``_base._tied_top``).

    ``plan`` reports it after every other reason, which says more.
    """

    config, text = runtime._config, str(question or "")
    subjects = _projected_subject_ids(query)
    measure = bool(subjects) and subjects[0].startswith("measure.")
    terms = set(intent_ir.target_measure_terms)
    canonical = (_canonical_measure if measure else _canonical_metric)(config, terms)
    if (
        len(subjects) != 1
        or subjects[0] in _projected_subject_ids(partial_query or {})
        or getattr(canonical, "id", None) == subjects[0]
        or _named_metric(config, text)
    ):
        return None
    tied, named = _tied_top(
        config.measures if measure else config.metric_recipes,
        terms,
        set(_tokens(_target_focus_text(text))) or terms,
    )
    ids = [row.id for row in tied]
    if len(ids) < 2 or subjects[0] not in ids or getattr(named, "id", None) == subjects[0]:
        return None
    candidates = " or ".join(f"{row.label} ({row.id})" for row in tied[:5])
    gap = CoverageGap(
        kind="subject_ambiguous",
        clause=_target_focus_text(text),
        message="The question fits these equally well, and the draft picked one of them.",
        expected={"candidates": ids[:5], "candidate_count": len(ids)},
        actual={"subjects": subjects},
        recovery_hint={
            "kind": "name_one_subject",
            "message": f"Ask again naming the one you mean: {candidates}.",
        },
    )
    return _coverage_why([gap])


def _time_block(query: dict[str, Any]) -> dict[str, Any]:
    time = query.get("time")
    return time if isinstance(time, dict) else {}


def _time_window_gaps(runtime: Any, text: str, query: dict[str, Any]) -> list[CoverageGap]:
    """The draft doesn't carry the window the question names, or carries another one."""

    expected = _time_bounds_from_text(text)
    if not expected:
        return []
    last = (expected.get("range") or {}).get("last") or {}
    # "alongside the previous month's revenue" names a prior-period
    # comparison's offset, not a window, when the draft carries one.
    if (
        set(expected) == {"range"}
        and last.get("value") == 1
        and _query_contains_prior_period(runtime, query)
    ):
        return []
    time = _time_block(query)
    carried = {key: time[key] for key in ("start", "end", "range") if time.get(key)}
    differs = [key for key in carried if carried[key] != expected.get(key)]
    missing = [key for key in expected if key not in carried]
    # A lookback metric can't take a start; plan says so itself
    # (TIME_WINDOW_START_DROPPED), keeping the end.
    if carried and not differs and missing in ([], ["start"]):
        return []
    return [
        CoverageGap(
            kind="time_window_unrealized",
            clause=", ".join(f"{key}={value}" for key, value in expected.items()),
            message=(
                "The question names a time window, but the draft's window is a different one."
                if carried
                else "The question names a time window, but the draft is not bounded by it."
            ),
            expected={"time": expected},
            actual={"time": time or None},
            recovery_hint={
                "kind": "provide_time_window",
                "message": (
                    "Add the window to Query IR time (start inclusive, end exclusive) with a grain "
                    "that yields the buckets the question asks for, then validate."
                ),
            },
        )
    ]


def _ranking_request(text: str, nouns: frozenset[str] = frozenset()) -> dict[str, Any] | None:
    """Parse a ranking request into (clause, limit, direction, noun, requires_order).

    ``limit`` is None when the question fixes no count ("the top products"),
    and ``direction`` is None when it fixes no order ("rank stores by
    revenue"). A bare superlative ranks the noun after it only when that noun
    is a time unit ("the highest revenue month"), follows a hyphenated
    superlative ("best-selling products"), or follows "best"/"worst" and is
    one of the catalog's dimension ``nouns`` ("the best store"): in "the
    highest revenue", revenue is what is measured, not what is ranked.
    """

    lowered = " ".join(str(text or "").lower().split())
    # "top-3 stores" is "top 3 stores".
    lowered = re.sub(r"\b(top|bottom|best|worst)-(\d+)\b", r"\1 \2", lowered)
    words = _WORD_RE.findall(lowered)
    for index in range(len(words)):
        request = _ranking_at(words, index, nouns)
        if request is not None:
            return request
    return None


def _ranking_at(words: list[str], index: int, nouns: frozenset[str]) -> dict[str, Any] | None:
    word = words[index]
    before = words[index - 1] if index else ""
    if word in {"rank", "ranking"}:
        # "rank stores by revenue", "ranking of the stores by revenue"
        start = index + 1
        while start < len(words) and words[start] in {"all", "of", "our", "the"}:
            start += 1
        noun, end = _noun_phrase(words, start)
        if noun and end < len(words) and words[end] == "by":
            return _ranking(words, index, end, None, _stated_direction(words, end), noun, True)
        return None
    if word == "ranked" and index + 1 < len(words) and words[index + 1] == "by":
        # "stores ranked by revenue"
        if before and before not in _NOT_RANKED:
            return _ranking(
                words, index - 1, index + 2, None, _stated_direction(words, index), before, True
            )
        return None
    if word in {"top", "bottom"}:
        # "top 5 products", "bottom three stores", "the top store", "top 2000 customers"
        direction: str | None = "DESC" if word == "top" else "ASC"
        cursor = index + 1
        count = _count(words, cursor, years=True)
        if count is not None:
            cursor += 1
        superlative = _superlative(words, cursor)
        if superlative:
            direction = superlative
            cursor += 1
        noun, end = _noun_phrase(words, cursor)
        if not noun:
            return None
        limit = count if count is not None else (1 if _singular(noun) == noun else None)
        return _ranking(words, index, end, limit, direction, noun, False)
    count = _count(words, index, years=False)
    if count is not None:
        # "the 3 lowest-selling products", "5 best-selling products",
        # "the 5 customers who spent the most"
        cursor = index + 1
        if _recency(words, cursor):
            return None
        superlative = _superlative(words, cursor)
        if superlative:
            cursor += 1
        noun, end = _noun_phrase(words, cursor)
        direction = superlative or _superlative_after(words, end)
        # "5 best-selling products", "the 3 stores with the least revenue",
        # "3 stores with the highest revenue" (a relative clause names the order).
        relative = end < len(words) and words[end] in _RELATIVE
        qualified = bool(superlative) or before == "the" or relative
        if not noun or not direction or not qualified:
            return None
        start = index - 1 if before == "the" else index
        return _ranking(words, start, end, count, direction, noun, False)
    if word == "the" and index + 1 < len(words) and words[index + 1] not in _NOT_RANKED:
        # "the store with the most orders", "the product that sold the least"
        noun, end = _noun_phrase(words, index + 1)
        if noun and words[end : end + 1] and words[end] in _RELATIVE:
            direction = _superlative_after(words, end)
            if direction:
                limit = 1 if _singular(noun) == noun else None
                return _ranking(words, index, end, limit, direction, noun, False)
    if word == "which":
        # "which store had the most orders", "which of the stores ...",
        # "which 2 stores ..."
        cursor = index + 1
        count = _count(words, cursor, years=False)
        if count is not None:
            cursor += 1
        one = words[cursor : cursor + 1] == ["of"] and cursor + 1 < len(words)
        if one:
            cursor += 2 if words[cursor + 1] in {"our", "the", "these", "those"} else 1
        noun, end = _noun_phrase(words, cursor)
        direction = _superlative_after(words, end)
        if not noun or not direction:
            return None
        limit = count if count is not None else (1 if one or _singular(noun) == noun else None)
        return _ranking(words, index, end, limit, direction, noun, False)
    superlative = _superlative(words, index)
    if superlative and _count(words, index + 1, years=True) is not None:
        # "best 3 stores by revenue", "highest 5 products"
        noun, end = _noun_phrase(words, index + 2)
        if noun:
            count = _count(words, index + 1, years=True)
            return _ranking(words, index, end, count, superlative, noun, False)
    if superlative:
        # "the best-selling product", "the highest revenue month", "the best store"
        noun, end = _noun_phrase(words, index + 1)
        head = _singular(noun)
        if noun and (
            "-" in word or head in _TIME_UNITS or (word in {"best", "worst"} and head in nouns)
        ):
            start = index - 1 if before == "the" else index
            return _ranking(
                words, start, end, 1 if head == noun else None, superlative, noun, False
            )
    return None


def _ranking(
    words: list[str],
    start: int,
    end: int,
    limit: int | None,
    direction: str | None,
    noun: str,
    requires_order: bool,
) -> dict[str, Any]:
    return {
        "clause": " ".join(words[start:end]),
        "limit": limit,
        "direction": direction,
        "noun": noun,
        "requires_order": requires_order,
    }


def _count(words: list[str], index: int, *, years: bool) -> int | None:
    """The count at words[index] ("5", "five"); a 20xx number counts only after top/bottom."""

    if index >= len(words):
        return None
    word = words[index]
    if word in _NUMBER_WORDS:
        return _NUMBER_WORDS[word]
    if not word.isdigit() or (not years and _YEAR_NUMBER_RE.fullmatch(word)):
        return None
    return int(word)


def _recency(words: list[str], index: int) -> bool:
    """ "most recent", "latest": an ordering by time, which a window answers."""

    if index >= len(words):
        return False
    if words[index] in _RECENCY:
        return True
    return words[index] in {"least", "most"} and words[index + 1 : index + 2] == ["recent"]


def _superlative(words: list[str], index: int) -> str | None:
    """The sort direction a superlative at words[index] asks for, if it ranks."""

    if index >= len(words) or _recency(words, index):
        return None
    base = words[index].split("-", 1)[0]
    if base not in _SUPERLATIVES:
        return None
    if base in {"least", "most"} and index and words[index - 1] == "at":
        return None  # "at least 10 orders" is a threshold
    return "ASC" if base in _ASCENDING else "DESC"


def _superlative_after(words: list[str], start: int) -> str | None:
    for index in range(start, len(words)):
        direction = _superlative(words, index)
        if direction:
            return direction
    return None


def _stated_direction(words: list[str], start: int) -> str | None:
    """The order a "rank ... by" request states, if any ("lowest first", "descending")."""

    for word in words[start:]:
        if word in {"asc", "ascending", "increasing"}:
            return "ASC"
        if word in {"desc", "descending", "decreasing"}:
            return "DESC"
    return _superlative_after(words, start)


def _noun_phrase(words: list[str], start: int) -> tuple[str, int]:
    """The ranked noun phrase at words[start]: its head word and the index after it."""

    end = start
    while (
        end < len(words)
        and end - start < 2
        and words[end] not in _NOT_RANKED
        and not words[end].isdigit()
    ):
        end += 1
    if end == start:
        return "", start
    head = words[end - 1]
    return (head[:-2] if head.endswith("'s") else head), end


def _singular(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _ranking_gaps(runtime: Any, text: str, query: dict[str, Any]) -> list[CoverageGap]:
    """A ranking request loses its limit, its sort or the thing being ranked."""

    config = runtime._config
    request = _ranking_request(text, _dimension_nouns(config))
    if request is None:
        return []
    order_by = [row for row in list(query.get("order_by") or []) if isinstance(row, dict)]
    problems: list[str] = []
    if request["limit"] is not None and query.get("limit") != request["limit"]:
        problems.append("limit")
    # Every ranking needs the requested order, even when no count was stated.
    # A draft's own limit can otherwise silently return the opposite end.
    first = order_by[0] if order_by else {}
    direction = str(first.get("direction") or "ASC").upper()
    if (
        not first
        or not _orders_by_a_value(first, query)
        or (request["direction"] and direction != request["direction"])
    ):
        problems.append("order")
    ranked_ids = _ranking_measure_ids(config, text, request)
    selected = [row for row in list(query.get("select") or []) if isinstance(row, dict)]
    ordered = next((row for row in selected if row.get("as") == first.get("field")), {})
    expression = ordered.get("expression")
    ordered_id = (
        expression.get("measure") or expression.get("metric")
        if isinstance(expression, dict)
        else None
    )
    if first and not ordered and "order" not in problems:
        problems.append("order")
    if len(ranked_ids) == 1 and ordered_id not in ranked_ids:
        problems.append("ranked_measure")
    elif len(ranked_ids) > 1 or (not ranked_ids and len(selected) > 1):
        problems.append("ranked_measure_uncertain")
    noun = _singular(request["noun"])
    time = _time_block(query)
    if noun in _TIME_UNITS:
        if str(time.get("grain", "") or "") != noun:
            problems.append("ranked_time_grain")
    else:
        ranked = {
            str(row.id)
            for row in config.dimensions
            if noun in {_singular(token) for token in _tokens(_object_text(row))}
        }
        grouped = {str(item) for item in list(query.get("group_by") or [])}
        if ranked and not ranked & grouped:
            problems.append("ranked_dimension")
    if not problems:
        return []
    return [
        CoverageGap(
            kind="ranking_unrealized",
            clause=request["clause"],
            message=(
                "The question asks for a ranking, but the draft does not return the requested "
                "rows in order: " + ", ".join(problems) + "."
            ),
            expected={
                "limit": request["limit"],
                "direction": request["direction"],
                "ranked": request["noun"],
                "order_by": "a selected value",
            },
            actual={
                "limit": query.get("limit"),
                "order_by": order_by,
                "group_by": list(query.get("group_by") or []),
                "grain": time.get("grain"),
            },
            recovery_hint={
                "kind": "provide_ranking",
                "message": (
                    "Group by the ranked dimension (or set time.grain for ranked periods), order "
                    "by the measure in the requested direction"
                    + (", and set the requested limit" if request["limit"] is not None else "")
                    + ", then validate."
                ),
            },
        )
    ]


def _orders_by_a_value(order: dict[str, Any], query: dict[str, Any]) -> bool:
    """Whether an order_by entry sorts by a selected value, not a group or the period."""

    name = order.get("field")
    if not isinstance(name, str):
        return True
    grouped = {str(item) for item in list(query.get("group_by") or [])}
    return name != "time" and name not in grouped and not name.startswith("dimension.")


def _ranking_measure_ids(config: Any, text: str, request: dict[str, Any]) -> set[str]:
    """Resolve an explicitly named ranking measure without guessing among catalog names."""

    normalized = re.sub(r"\b(top|bottom|best|worst)-(\d+)\b", r"\1 \2", text.lower())
    words = _WORD_RE.findall(normalized)
    clause = request["clause"].split()
    start = next(
        (
            index + len(clause)
            for index in range(len(words))
            if words[index : index + len(clause)] == clause
        ),
        len(words),
    )
    tail = words[start:]
    if not clause or clause[-1] != "by":
        anchor = next(
            (
                index
                for index, word in enumerate(tail)
                if word in {"by", "most", "least", "highest", "lowest", "fewest"}
            ),
            None,
        )
        if anchor is None:
            return set()
        tail = tail[anchor + 1 :]
    while tail and tail[0] in {"the", "total"}:
        tail = tail[1:]
    phrase: list[str] = []
    for word in tail:
        if word in _PHRASE_BREAKS | {"among", "except", "excluding", "vs", "versus", "but"}:
            break
        phrase.append(word)
    sought = tuple(_singular(word) for word in _plain(" ".join(phrase)).split())
    if not sought:
        return set()
    matched: set[str] = set()
    for row in [*getattr(config, "measures", []), *getattr(config, "metric_recipes", [])]:
        object_id = str(getattr(row, "id", "") or "")
        fields = [
            object_id.rsplit(".", 1)[-1],
            str(getattr(row, "name", "") or "").rsplit(".", 1)[-1],
            str(getattr(row, "label", "") or ""),
            *[str(alias) for alias in getattr(row, "aliases", []) or []],
        ]
        for candidate in fields:
            tokens = [_singular(word) for word in _plain(candidate).split()]
            while tokens and tokens[-1] in {"usd"}:
                tokens.pop()
            if tuple(tokens) == sought:
                matched.add(object_id)
                break
    return matched


def _dimension_nouns(config: Any) -> frozenset[str]:
    return frozenset(
        _singular(token)
        for row in list(getattr(config, "dimensions", []) or [])
        for token in _tokens(_core_text(row))
    )


def _filter_value_gaps(runtime: Any, text: str, query: dict[str, Any]) -> list[CoverageGap]:
    """Every governed value the question names must reach the draft.

    A value is honored by a filter with the requested polarity, by
    grouping on its dimension when no filter drops it, or by a chosen object
    whose name carries the question's word for it ("new customer orders"
    answered by a new-customer measure). A filter that also keeps an unnamed
    value in an ungrouped total, or drops an unnamed value, is not. Longer
    values mask the words inside them ("New Orleans" is not "new"). Numbers,
    and everyday words not tied to their dimension in the question, are left
    to the unmatched-terms warning.
    """

    config = runtime._config
    phrases = _value_phrases(config)
    plain = _plain(text)
    matches = _value_matches(plain, phrases)
    if not matches:
        return []
    predicates = _field_predicates(query)
    # Every value the question names, in either polarity, by dimension. An
    # ungrouped total keeps only these, and an exclusion drops only these.
    named: dict[str, list[Any]] = {}
    for _span, phrase in matches:
        for domain, value in phrases[phrase]:
            for dimension in domain.dimensions:
                named.setdefault(str(dimension), []).append(value.value)
    # Keep punctuation and explicit inclusion transitions when assigning
    # polarity. _plain removes both, so its offsets cannot define a clause.
    source_words = list(re.finditer(r"[^\W_]+", text.lower()))
    excluded_spans = _excluded_value_spans(text)
    grouped = {str(item) for item in list(query.get("group_by") or [])}
    referenced = set(_referenced_ids(query))
    carried = {
        token
        for row in _catalog_rows(config)
        if str(getattr(row, "id", "")) in referenced
        for token in _tokens(_core_text(row))
    }
    said: list[str] = []
    missing: list[dict[str, Any]] = []
    for span, phrase in matches:
        rows = phrases[phrase]
        first_word = plain.count(" ", 0, span[0])
        last_word = first_word + plain[span[0] : span[1]].count(" ")
        original_span = (source_words[first_word].start(), source_words[last_word].end())
        negative = any(
            start <= original_span[0] and original_span[1] <= end for start, end in excluded_spans
        )
        if phrase in _EVERYDAY_WORDS and not _tied_to_dimension(config, plain, span, rows):
            continue
        if any(
            _value_honored(domain, value, predicates, grouped, negative, named)
            for domain, value in rows
        ):
            continue
        # The dedicated negation check already reports a missing negative
        # predicate or a positive predicate on this excluded value.
        if negative and (
            not _query_has_negative_semantics(query)
            or _positive_filter_evidence(runtime, query, phrase)
        ):
            continue
        relevant_filter = any(
            str(dimension) in predicates
            for domain, value in rows
            for dimension in domain.dimensions
        )
        if not negative and not relevant_filter and set(_tokens(phrase)) <= carried:
            continue
        said.append(phrase)
        for domain, value in rows:
            entry = next((row for row in missing if row["value"] == value.value), None)
            if entry is None:
                missing.append({"value": value.value, "dimensions": list(domain.dimensions)})
            else:
                entry["dimensions"].extend(
                    item for item in domain.dimensions if item not in entry["dimensions"]
                )
    if not missing:
        return []
    return [
        CoverageGap(
            kind="filter_values_unrealized",
            clause=", ".join(said),
            message="The draft does not preserve the requested inclusion or exclusion of named values.",
            expected={"values": missing},
            actual={
                "where": list(query.get("where") or []),
                "group_by": list(query.get("group_by") or []),
            },
            recovery_hint={
                "kind": "provide_filter_values",
                "message": (
                    "Use a filter with the requested polarity for every named value "
                    "(op 'in' for several included values of one dimension), then validate."
                ),
            },
        )
    ]


# "where channel is web" filters on a dimension it names, whether or not the
# catalog declares the value.
_WHERE_FIELD_RE = re.compile(
    r"\bwhere\s+(?:the\s+)?(?P<field>[a-z][a-z0-9 _-]*?)\s*"
    r"(?:\b(?:is|are|was|were|equals?)\b|[!=<>]=?)",
    re.IGNORECASE,
)


def _where_clause_gaps(runtime: Any, text: str, query: dict[str, Any]) -> list[CoverageGap]:
    """A "where <dimension> is <value>" clause needs a filter on that dimension."""

    predicates = _field_predicates(query)
    gaps: list[CoverageGap] = []
    for match in _WHERE_FIELD_RE.finditer(text):
        said = _singular(_plain(match.group("field")))
        fields = [
            str(row.id)
            for row in runtime._config.dimensions
            if said in {_singular(_plain(name)) for name in (row.label, *(row.aliases or []))}
        ]
        if fields and not any(field in predicates for field in fields):
            gaps.append(
                CoverageGap(
                    kind="dimension_filter_unrealized",
                    clause=match.group(0),
                    message="The question filters on a dimension it names, but the draft doesn't.",
                    expected={"filter_on": fields},
                    actual={"where": list(query.get("where") or [])},
                    recovery_hint={
                        "kind": "provide_dimension_filter",
                        "message": (
                            "Add a where filter on the named dimension, with a value from "
                            "valid_values, then validate."
                        ),
                    },
                )
            )
    return gaps


def _exclusion_matches(text: str) -> list[re.Match[str]]:
    return sorted(
        (match for pattern in (_NEGATION_RE, _ALL_BUT_RE) for match in pattern.finditer(text)),
        key=lambda match: match.start(),
    )


def _excluded_value_spans(text: str) -> list[tuple[int, int]]:
    """Negative clauses include comma lists, ending at an explicit inclusion."""

    spans: list[tuple[int, int]] = []
    matches = _exclusion_matches(text)
    for index, match in enumerate(matches):
        start = match.start("value")
        tail = text[start:]
        # In "not including Brooklyn", the first "including" completes
        # the exclusion; only a later one opens a positive clause.
        initial = re.match(r"(?:including|include)\b", tail, re.IGNORECASE)
        scan_from = initial.end() if initial else 0
        stop = re.search(r"[.;!?]|\b(?:including|include)\b", tail[scan_from:], re.IGNORECASE)
        end = start + scan_from + stop.start() if stop else len(text)
        if index + 1 < len(matches):
            end = min(end, matches[index + 1].start())
        spans.append((start, end))
    return spans


def _plain(text: Any) -> str:
    """Lowercase words separated by single spaces ("High_Value" -> "high value")."""

    return " ".join(re.findall(r"[^\W_]+", str(text or "").lower()))


def _value_phrases(config: Any) -> dict[str, list[tuple[Any, Any]]]:
    """Each way the catalog writes a value, with the (domain, value) pairs it names."""

    out: dict[str, list[tuple[Any, Any]]] = {}
    for domain in list(getattr(config, "value_domains", []) or []):
        for value in list(domain.values or []):
            if _is_number(value.value):
                continue
            names = {_plain(item) for item in (value.value, value.label, *(value.aliases or []))}
            for phrase in names:
                if len(phrase) >= 2 and not _is_number(phrase):
                    out.setdefault(phrase, []).append((domain, value))
    return out


def _value_names(value: Any) -> list[str]:
    return [str(item) for item in (value.value, value.label, *(value.aliases or [])) if item]


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    return bool(re.fullmatch(r"[\d\s.,]+", str(value)))


def _value_matches(
    plain: str, phrases: dict[str, list[tuple[Any, Any]]]
) -> list[tuple[tuple[int, int], str]]:
    """Value mentions in the question, longest first, never overlapping."""

    words = set(plain.split())
    taken: list[tuple[int, int]] = []
    found: list[tuple[tuple[int, int], str]] = []
    for phrase in sorted(phrases, key=len, reverse=True):
        parts = phrase.split()
        if not set(parts[:-1]) <= words or not {parts[-1], f"{parts[-1]}s", f"{parts[-1]}es"} & (
            words
        ):
            continue
        for match in re.finditer(rf"(?<!\S){re.escape(phrase)}(?:e?s)?(?!\S)", plain):
            span = match.span()
            if not any(span[0] < end and start < span[1] for start, end in taken):
                taken.append(span)
                found.append((span, phrase))
    return sorted(found)


def _tied_to_dimension(
    config: Any, plain: str, span: tuple[int, int], rows: list[tuple[Any, Any]]
) -> bool:
    """An everyday word sits next to a word of its value's dimension ("new customers")."""

    neighbors = plain[: span[0]].split()[-1:] + plain[span[1] :].split()[:1]
    near = {_singular(token) for token in _tokens(" ".join(neighbors))}
    dimensions = {str(item) for domain, _value in rows for item in domain.dimensions}
    generic = _GENERIC_ID_WORDS | _ubiquitous_words(config)
    words = {
        _singular(token)
        for row in config.dimensions
        if str(row.id) in dimensions
        for token in _tokens(_core_text(row))
    } - generic
    # A shared stem of four letters or more ties them too ("members", "membership").
    return any(
        word == other or (min(len(word), len(other)) >= 4 and word.startswith(other))
        for word in words
        for other in near
    )


def _ubiquitous_words(config: Any) -> set[str]:
    """Words in most dimension ids, such as a package prefix, which say nothing."""

    rows = list(getattr(config, "dimensions", []) or [])
    counts = Counter(token for row in rows for token in set(_tokens(_core_text(row))))
    return {token for token, count in counts.items() if count * 2 > len(rows)}


@dataclass
class _FieldConstraints:
    """One query-level field's conjunctive predicates, in executable literals."""

    keeping: list[list[Any]] = field(default_factory=list)
    dropping: list[list[Any]] = field(default_factory=list)
    uncertain: bool = False

    def kept_literals(self) -> list[Any] | None:
        """Values admitted by every keeping predicate, if bounded."""

        if not self.keeping:
            return None
        candidates = list(self.keeping[0])
        for choices in self.keeping[1:]:
            candidates = [item for item in candidates if _contains_literal(choices, item)]
        return candidates

    def surviving_literals(self) -> list[Any] | None:
        """Values admitted by every known top-level predicate, if bounded."""

        kept = self.kept_literals()
        if kept is None:
            return None
        return [
            item
            for item in kept
            if not any(_contains_literal(choices, item) for choices in self.dropping)
        ]

    def keeps(self, canonical: Any, *, grouped: bool, named: list[Any] | None = None) -> bool:
        """The value survives. Given the question's ``named`` values, an ungrouped
        draft is one total, so it must keep no other value."""

        if self.uncertain:
            return False
        survivors = self.surviving_literals()
        if survivors is None:
            return grouped and not self.drops(canonical)
        return _contains_literal(survivors, canonical) and (
            named is None or grouped or all(_contains_literal(named, item) for item in survivors)
        )

    def drops(self, canonical: Any, *, named: list[Any] | None = None) -> bool:
        """The value is dropped. Given the question's ``named`` values, no other
        value that would otherwise survive is dropped too."""

        if self.uncertain or not any(
            _contains_literal(choices, canonical) for choices in self.dropping
        ):
            return False
        kept = self.kept_literals()
        return named is None or all(
            _contains_literal(named, item)
            or (kept is not None and not _contains_literal(kept, item))
            for choices in self.dropping
            for item in choices
        )


def _contains_literal(literals: list[Any], canonical: Any) -> bool:
    """SQL string equality has no catalog-label, case or punctuation rewrite."""

    return any(type(item) is type(canonical) and item == canonical for item in literals)


def _field_predicates(query: dict[str, Any]) -> dict[str, _FieldConstraints]:
    """Build query-level AND constraints; nested expression scopes remain unproven."""

    out: dict[str, _FieldConstraints] = {}
    for node in list(query.get("where") or []):
        if not isinstance(node, dict) or not isinstance(node.get("field"), str):
            continue
        name = node["field"]
        entry = out.setdefault(name, _FieldConstraints())
        op = " ".join(str(node.get("op") or "=").upper().split())
        literals = _membership_literals(op, node.get("value"))
        if literals is None:
            entry.uncertain = True
        elif op in _KEEPING_OPS:
            entry.keeping.append(literals)
        elif op in _EXCLUDING_OPS:
            entry.dropping.append(literals)
    # A selected expression can have its own where/predicate scope. Its rows
    # are not interchangeable with query-level where rows, so do not union or
    # intersect its values with the outer filter (or credit grouping through it).
    nested = {key: value for key, value in query.items() if key != "where"}
    for node in _dict_nodes(nested):
        name = node.get("field")
        if isinstance(name, str) and ("op" in node or "value" in node):
            out.setdefault(name, _FieldConstraints()).uncertain = True
    return out


def _membership_literals(op: str, raw: Any) -> list[Any] | None:
    """Only exact scalar comparisons and membership ops prove named-value polarity."""

    if op in {"IN", "NOT IN"}:
        literals = raw if isinstance(raw, list) else [raw]
    elif op in (_KEEPING_OPS | _EXCLUDING_OPS) and not isinstance(raw, (list, tuple, dict)):
        literals = [raw]
    else:
        return None
    if not literals or any(
        item is None or isinstance(item, (list, tuple, dict)) for item in literals
    ):
        return None
    return literals


def _value_honored(
    domain: Any,
    value: Any,
    predicates: dict[str, _FieldConstraints],
    grouped: set[str],
    negative: bool,
    named: dict[str, list[Any]],
) -> bool:
    """A filter has the requested polarity without keeping or dropping values the
    question doesn't name, or grouping keeps a positive value."""

    canonical = value.value
    for dimension in (str(item) for item in domain.dimensions):
        entry = predicates.get(dimension)
        names = named.get(dimension, [])
        if entry is not None:
            if negative and entry.drops(canonical, named=names):
                return True
            if not negative and entry.keeps(canonical, grouped=dimension in grouped, named=names):
                return True
        elif not negative and dimension in grouped:
            return True
    return False


def _contradictory_filter_gaps(query: dict[str, Any]) -> list[CoverageGap]:
    """Query-level conjunctions that leave no kept literal after exclusions."""

    conflicts: list[dict[str, Any]] = []
    for name, entry in _field_predicates(query).items():
        survivors = entry.surviving_literals()
        if survivors is None or survivors:
            continue
        conflicts.append(
            {
                "field": name,
                "values": sorted({str(item) for group in entry.keeping for item in group}),
            }
        )
    if not conflicts:
        return []
    return [
        CoverageGap(
            kind="contradictory_filters",
            clause="; ".join(
                f"{row['field']} = " + " and ".join(row["values"]) for row in conflicts
            ),
            message=(
                "The draft's filters on one field cannot retain any value together, so it "
                "returns no rows."
            ),
            expected={"filters": "one filter per field, op 'in' for several values"},
            actual={"conflicts": conflicts},
            recovery_hint={
                "kind": "merge_filter_values",
                "message": (
                    "Filter each dimension once, with op 'in' and a list for several values (or "
                    "group by it to compare them), then validate."
                ),
            },
        )
    ]


def _core_text(row: Any) -> str:
    """An object's id, name, label and aliases: the words that name it."""

    return " ".join(
        [
            str(getattr(row, "id", "") or ""),
            str(getattr(row, "name", "") or ""),
            str(getattr(row, "label", "") or ""),
            " ".join(str(alias) for alias in getattr(row, "aliases", []) or []),
        ]
    )


def _catalog_rows(config: Any) -> list[Any]:
    return [
        *getattr(config, "measures", []),
        *getattr(config, "metric_recipes", []),
        *getattr(config, "dimensions", []),
        *getattr(config, "entities", []),
        *getattr(config, "segments", []),
        *getattr(config, "temporal_roles", []),
    ]


def _referenced_ids(query: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for node in _dict_nodes(query):
        for key in ("measure", "metric", "field", "temporal_role", "entity", "segment"):
            value = node.get(key)
            if isinstance(value, str) and value and value not in ids:
                ids.append(value)
    ids.extend(str(item) for item in list(query.get("group_by") or []) if str(item) not in ids)
    return ids


def _dict_nodes(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _dict_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _dict_nodes(child)


def _query_has_partitioned_ranking(query: dict[str, Any]) -> bool:
    for node in _dict_nodes(query):
        partition = node.get("partition_by")
        if partition not in (None, "", [], {}):
            kind = str(node.get("kind", "") or "").casefold()
            if kind in {"rank", "ranking", "top_n", "row_number"} or any(
                key in node for key in ("order_by", "limit", "rank")
            ):
                return True
    return False


def _query_contains_prior_period(runtime: Any, query: dict[str, Any]) -> bool:
    from ..expressions import PriorPeriodExpr  # local import keeps planner startup light

    metric_ids: set[str] = set()
    for node in _dict_nodes(query):
        if str(node.get("kind", "") or "").casefold() == "prior_period":
            return True
        metric_id = node.get("metric") or node.get("metric_recipe")
        if isinstance(metric_id, str) and metric_id:
            metric_ids.add(metric_id)
    for recipe in getattr(runtime._config, "metric_recipes", []) or []:
        if str(getattr(recipe, "id", "") or "") not in metric_ids:
            continue
        if isinstance(getattr(recipe, "expression", None), PriorPeriodExpr):
            return True
    return False


def _query_has_negative_semantics(query: dict[str, Any]) -> bool:
    for node in _dict_nodes(query):
        kind = str(node.get("kind", "") or "").casefold()
        if kind in {"not", "not_in", "not_between"} or node.get("negated") is True:
            return True
        op = " ".join(str(node.get("op", "") or "").upper().split())
        if op in _NEGATIVE_OPS:
            return True
        value = node.get("value")
        if value is False or (value == 0 and op in {"=", "<", "<="}):
            return True
    return False


def _positive_filter_evidence(
    runtime: Any, query: dict[str, Any], excluded_text: str
) -> list[dict[str, Any]]:
    """Positive outer filters that actually retain a canonical excluded value."""

    constraints = _field_predicates(query)
    phrases = _value_phrases(runtime._config)
    out: list[dict[str, Any]] = []
    for _span, phrase in _value_matches(_plain(excluded_text), phrases):
        for domain, value in phrases[phrase]:
            for dimension in (str(item) for item in domain.dimensions):
                entry = constraints.get(dimension)
                if entry is None or not entry.keeps(value.value, grouped=False):
                    continue
                for row in list(query.get("where") or []):
                    if not isinstance(row, dict) or row.get("field") != dimension:
                        continue
                    op = " ".join(str(row.get("op") or "=").upper().split())
                    literals = _membership_literals(op, row.get("value"))
                    if (
                        op in _KEEPING_OPS
                        and literals
                        and _contains_literal(literals, value.value)
                        and row not in out
                    ):
                        out.append(row)
    return out


def _selected_expression_kinds(query: dict[str, Any]) -> list[str]:
    kinds: list[str] = []
    for item in list(query.get("select") or []):
        expression = item.get("expression") if isinstance(item, dict) else None
        if not isinstance(expression, dict):
            continue
        kind = str(expression.get("kind", "") or "")
        if not kind:
            kind = "metric" if expression.get("metric") else "measure"
        if kind and kind not in kinds:
            kinds.append(kind)
    return kinds


def _projected_subject_ids(query: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in list(query.get("select") or []):
        expression = item.get("expression") if isinstance(item, dict) else None
        if not isinstance(expression, dict):
            continue
        object_id = expression.get("metric") or expression.get("measure")
        if isinstance(object_id, str) and object_id and object_id not in out:
            out.append(object_id)
    return out


def _conjoined_subjects(runtime: Any, text: str) -> list[dict[str, Any]]:
    """Return exact catalog subjects conjoined in the target phrase.

    This intentionally refuses fuzzy resolution.  Each side must exactly match
    an authored id suffix, name, label, or alias after harmless request words
    are removed.  Qualification clauses are cut off before splitting so
    supported "target for stores with orders and sessions" patterns do not
    become false multi-select requests.
    """

    target_text = _SUBJECT_BOUNDARY_RE.split(str(text or ""), maxsplit=1)[0]
    pieces = [
        piece.strip() for piece in _SUBJECT_CONJUNCTION_RE.split(target_text) if piece.strip()
    ]
    if len(pieces) < 2:
        return []
    rows = [
        *getattr(runtime._config, "measures", []),
        *getattr(runtime._config, "metric_recipes", []),
    ]
    matches: list[dict[str, Any]] = []
    for piece in pieces:
        piece_tokens = _subject_tokens(piece)
        if not piece_tokens:
            return []
        candidate_ids: list[str] = []
        for row in rows:
            if _matches_exact_subject_field(row, piece_tokens):
                object_id = str(getattr(row, "id", "") or "")
                if object_id and object_id not in candidate_ids:
                    candidate_ids.append(object_id)
        if not candidate_ids:
            return []
        matches.append({"phrase": piece, "candidate_ids": candidate_ids})
    return matches


def _matches_exact_subject_field(row: Any, piece_tokens: tuple[str, ...]) -> bool:
    object_id = str(getattr(row, "id", "") or "")
    id_suffix = object_id.rsplit(".", 1)[-1]
    fields = (
        object_id,
        id_suffix,
        str(getattr(row, "name", "") or ""),
        str(getattr(row, "label", "") or ""),
        *[str(value) for value in list(getattr(row, "aliases", []) or [])],
    )
    # Filler goes on both sides: "order count" names the "Order count" measure.
    return any(_subject_tokens(value) == piece_tokens for value in fields if value)


def _subject_tokens(text: str) -> tuple[str, ...]:
    return tuple(token for token in _tokens(text) if token not in _SUBJECT_FILLER)


def _unique_hints(gaps: list[CoverageGap]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for gap in gaps:
        hint = gap.recovery_hint
        key = str(hint.get("kind", "") or "")
        if not hint or key in seen:
            continue
        seen.add(key)
        out.append(dict(hint))
    return out


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


def unmatched_intent_terms(runtime: Any, question: str, query: dict[str, Any]) -> list[str]:
    """Question words the draft accounts for nowhere, in question order.

    A word is accounted for when it frames the question, sits in a time phrase
    the planner read, counts or orders ("five", "3rd"), or appears (allowing a
    plural or one typo) in the text of an object the draft uses or in one of
    its filter values. Words come back as the question spells them, at most
    eight.
    """

    from ..metadata_parts.relevance import _INTENT_STOPWORDS  # noqa: WPS433

    referenced = set(_referenced_ids(query))
    vocabulary: set[str] = set()
    for row in _catalog_rows(runtime._config):
        if str(getattr(row, "id", "")) in referenced:
            vocabulary.update(_tokens(_object_text(row)))
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
    time_spans = _time_window(text).spans
    seen: set[str] = set()
    out: list[str] = []
    for match in re.finditer(r"[^\W_]+", text.lower()):
        word = match.group(0)
        if word in seen:
            continue
        seen.add(word)
        if len(seen) > _MAX_SCANNED_WORDS or len(out) >= _MAX_UNMATCHED_TERMS:
            break
        token = _TERM_SYNONYMS.get(word, word)
        start, end = match.span()
        if (
            len(word) < 2
            or word.isdigit()
            or _ORDINAL_RE.fullmatch(word)
            or word in skipped
            or token in skipped
            or token in vocabulary
            or _singular(token) in vocabulary
            or any(start < span_end and span_start < end for span_start, span_end in time_spans)
            or _one_typo_away(token, by_initial)
        ):
            continue
        out.append(word)
    return out


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


__all__ = ["CoverageGap", "intent_faithfulness_why", "unmatched_intent_terms"]
