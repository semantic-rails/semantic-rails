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
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Any

from ._base import _MONTH_NUMBERS, _TIME_UNITS, _object_text, _time_bounds_from_text, _tokens
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
    r"\b(?:compared\s+(?:with|to)|vs\.?|versus)\s+(?:the\s+)?"
    r"(?:last|prior|previous)\s+(?:day|week|month|quarter|year|period)\b",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(
    r"\b(?P<marker>excluding|except|without|but\s+not|not)\s+"
    r"(?!only\b)(?P<value>[^,.;]+)",
    re.IGNORECASE,
)
# Ranking requests: "top 5 products", "the 3 lowest-selling products", "the 5
# customers who spent the most", "which store had the most orders".
# "at least 10 orders" is a threshold, not a ranking.
_SUPERLATIVE_RE = re.compile(
    r"\b(?:top|bottom|highest|lowest|fewest|largest|smallest|biggest|best|worst|greatest)\b"
    r"|(?<!at )\b(?:most|least)\b",
    re.IGNORECASE,
)
_ASCENDING_RE = re.compile(
    r"\b(?:bottom|lowest|fewest|smallest|worst)\b|(?<!at )\bleast\b", re.IGNORECASE
)
_RANKED_NOUN_RE = re.compile(
    r"\b(?:(?:top|bottom)\s+(?P<top>\d+)|the\s+(?P<count>\d+)|which(?:\s+(?P<which>\d+))?)\s+"
    r"(?:(?:highest|lowest|most|least|best|worst|largest|smallest|biggest|greatest|fewest)"
    r"(?:-[a-z]+)?\s+)?(?P<noun>[a-z][a-z-]*)(?:\s+(?P<noun2>[a-z][a-z-]*))?",
    re.IGNORECASE,
)
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
_YEAR_NUMBER_RE = re.compile(r"^20\d{2}$")
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
        "list",
        "me",
        "monthly",
        "of",
        "please",
        "quarterly",
        "show",
        "sum",
        "the",
        "total",
        "weekly",
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


def intent_faithfulness_why(
    runtime: Any,
    *,
    question: str,
    intent_ir: IntentIR,
    query: dict[str, Any],
) -> dict[str, Any] | None:
    """Return a structured downgrade reason for high-confidence coverage gaps."""

    gaps: list[CoverageGap] = []

    text = str(question or "")
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

    negation_match = _NEGATION_RE.search(text)
    if negation_match:
        excluded_text = negation_match.group("value").strip()
        positive_filters = _positive_filter_evidence(query, excluded_text)
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

    gaps.extend(_time_window_gaps(text, query))
    gaps.extend(_ranking_gaps(runtime, text, query))
    gaps.extend(_filter_value_gaps(runtime, text, query))

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


def _time_block(query: dict[str, Any]) -> dict[str, Any]:
    time = query.get("time")
    return time if isinstance(time, dict) else {}


def _time_window_gaps(text: str, query: dict[str, Any]) -> list[CoverageGap]:
    """The question names a calendar or relative window the draft doesn't carry."""

    expected = _time_bounds_from_text(text)
    time = _time_block(query)
    if not expected or any(time.get(key) for key in ("start", "end", "range")):
        return []
    return [
        CoverageGap(
            kind="time_window_unrealized",
            clause=", ".join(f"{key}={value}" for key, value in expected.items()),
            message="The question names a time window, but the draft is not bounded by it.",
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


def _ranking_request(text: str) -> dict[str, Any] | None:
    """Parse "top N <noun>"-style requests into (limit, direction, noun)."""

    if not _SUPERLATIVE_RE.search(text):
        return None
    match = next(
        (
            candidate
            for candidate in _RANKED_NOUN_RE.finditer(text)
            if not _YEAR_NUMBER_RE.match(
                candidate.group("top") or candidate.group("count") or candidate.group("which") or ""
            )
        ),
        None,
    )
    if match is None:
        return None
    words = [match.group("noun").lower()]
    second = (match.group("noun2") or "").lower()
    if second and second not in _PHRASE_BREAKS:
        words.append(second)
    head = words[-1]
    raw_limit = match.group("top") or match.group("count") or match.group("which")
    if raw_limit:
        limit: int | None = int(raw_limit)
    else:
        # "which store had the most" asks for one; "which segments" for all of them.
        limit = 1 if _singular(head) == head else None
    phrase = match.group(0).split()
    return {
        "clause": " ".join(phrase[:-1] if second in _PHRASE_BREAKS else phrase),
        "limit": limit,
        "direction": "ASC" if _ASCENDING_RE.search(text) else "DESC",
        "noun": head,
    }


def _singular(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _ranking_gaps(runtime: Any, text: str, query: dict[str, Any]) -> list[CoverageGap]:
    """A ranking request loses its limit, its sort or the thing being ranked."""

    request = _ranking_request(text)
    if request is None:
        return []
    order_by = [row for row in list(query.get("order_by") or []) if isinstance(row, dict)]
    problems: list[str] = []
    # Order only decides the answer when a limit cuts it off.
    if request["limit"] is not None:
        if query.get("limit") != request["limit"]:
            problems.append("limit")
        direction = str(order_by[0].get("direction", "ASC")).upper() if order_by else ""
        if direction != request["direction"]:
            problems.append("order")
    noun = _singular(request["noun"])
    time = _time_block(query)
    if noun in _TIME_UNITS:
        if str(time.get("grain", "") or "") != noun:
            problems.append("ranked_time_grain")
    else:
        config = runtime._config
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
                    "by the measure in the requested direction, and set limit, then validate."
                ),
            },
        )
    ]


def _filter_value_gaps(runtime: Any, text: str, query: dict[str, Any]) -> list[CoverageGap]:
    """Every governed value the question names must reach a filter.

    A value also counts as honored when the draft's chosen objects carry it
    (for example "new customer orders" answered by a new-customer measure).
    """

    lowered = str(text or "").lower()
    filtered: set[str] = set()
    for row in list(query.get("where") or []):
        if isinstance(row, dict):
            value = row.get("value")
            for item in value if isinstance(value, list) else [value]:
                filtered.add(str(item).lower())
    config = runtime._config
    referenced = set(_referenced_ids(query))
    object_tokens: set[str] = set()
    for row in [*config.measures, *config.metric_recipes, *config.dimensions]:
        if str(getattr(row, "id", "")) in referenced:
            object_tokens.update(_tokens(_object_text(row)))
    missing: list[dict[str, Any]] = []
    for domain in config.value_domains:
        for value in list(domain.values or []):
            phrases = [str(value.value), str(value.label), *[str(a) for a in value.aliases or []]]
            phrases = [phrase.strip().lower() for phrase in phrases if str(phrase).strip()]
            if not any(
                re.search(rf"(?<![a-z0-9]){re.escape(phrase)}s?(?![a-z0-9])", lowered)
                for phrase in phrases
            ):
                continue
            if filtered & set(phrases) or any(set(_tokens(p)) <= object_tokens for p in phrases):
                continue
            if all(row["value"] != value.value for row in missing):
                missing.append({"value": value.value, "dimensions": list(domain.dimensions)})
    if not missing:
        return []
    return [
        CoverageGap(
            kind="filter_values_unrealized",
            clause=", ".join(str(row["value"]) for row in missing),
            message="The question names values that no filter in the draft uses.",
            expected={"values": missing},
            actual={"where": list(query.get("where") or [])},
            recovery_hint={
                "kind": "provide_filter_values",
                "message": (
                    "Filter on every named value (op 'in' with a list for several values of one "
                    "dimension), then validate."
                ),
            },
        )
    ]


def _referenced_ids(query: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for node in _dict_nodes(query):
        for key in ("measure", "metric", "field", "temporal_role"):
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


def _positive_filter_evidence(query: dict[str, Any], excluded_text: str) -> list[dict[str, Any]]:
    lowered = excluded_text.casefold()
    out: list[dict[str, Any]] = []
    for row in list(query.get("where") or []):
        if not isinstance(row, dict):
            continue
        op = " ".join(str(row.get("op", "") or "").upper().split())
        if op not in {"=", "==", "IN", "IS"}:
            continue
        values = row.get("value")
        items = values if isinstance(values, list) else [values]
        if any(str(value).casefold() in lowered for value in items if value not in (None, "")):
            out.append(
                {"field": row.get("field") or row.get("dimension"), "op": op, "value": values}
            )
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
        piece_tokens = tuple(token for token in _tokens(piece) if token not in _SUBJECT_FILLER)
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
        id_suffix,
        str(getattr(row, "name", "") or ""),
        str(getattr(row, "label", "") or ""),
        *[str(value) for value in list(getattr(row, "aliases", []) or [])],
    )
    return any(tuple(_tokens(value)) == piece_tokens for value in fields if value)


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
# words, ranking and calendar vocabulary, and generic aggregation words.
_FRAMING_WORDS = frozenset(
    {
        *[
            "all",
            "amount",
            "be",
            "been",
            "can",
            "compare",
            "could",
            "display",
            "each",
            "every",
            "find",
            "give",
            "group",
            "grouped",
            "list",
            "me",
            "need",
            "number",
            "please",
            "see",
            "sum",
            "total",
            "totals",
            "overall",
            "want",
            "whose",
            # Verbs that restate a measure ("tax collected", "customers who spent").
            "brought",
            "collected",
            "earned",
            "generated",
            "had",
            "made",
            "spent",
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
            "first",
            "second",
            "last",
        ],
        *_MONTH_NUMBERS,
    }
)


def unmatched_intent_terms(runtime: Any, question: str, query: dict[str, Any]) -> list[str]:
    """Question words the draft accounts for nowhere, in question order.

    A word is accounted for when it frames the question, or appears (allowing
    for a plural or a near-miss spelling) in the text of an object the draft
    uses or in one of its filter values.
    """

    from ..metadata_parts.relevance import _INTENT_STOPWORDS  # noqa: WPS433

    config = runtime._config
    referenced = set(_referenced_ids(query))
    vocabulary: set[str] = set()
    for row in [*config.measures, *config.metric_recipes, *config.dimensions]:
        if str(getattr(row, "id", "")) in referenced:
            vocabulary.update(_tokens(_object_text(row)))
    for row in list(query.get("where") or []):
        if isinstance(row, dict):
            value = row.get("value")
            for item in value if isinstance(value, list) else [value]:
                vocabulary.update(_tokens(str(item)))
    known = sorted(vocabulary)
    out: list[str] = []
    for token in _tokens(question):
        if (
            len(token) < 2
            or token.isdigit()
            or token in out
            or token in _INTENT_STOPWORDS
            or token in _FRAMING_WORDS
            or token in vocabulary
            or _singular(token) in vocabulary
            or get_close_matches(token, known, n=1, cutoff=0.8)
        ):
            continue
        out.append(token)
    return out


__all__ = ["CoverageGap", "intent_faithfulness_why", "unmatched_intent_terms"]
