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
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..ast import _relative_range_bounds, is_child_group
from ..compiler import bind_query
from ..compiler_parts.sql_lowering import _snapshot_series_columns
from ..config_parts.measure_governance import (
    building_block_measures,
    governing_metrics,
    population_governors,
    published_measure,
)
from ..errors import SemanticLayerError
from ..expressions import expr_to_dict
from ..visible_view import base_of, hidden_on_its_own
from ._base import (
    _BOUNDARY_BEFORE_RE,
    _FISCAL_BUCKET_RE,
    _FISCAL_RE,
    _MAX_TIME_TEXT,
    _MONTH_NUMBERS,
    _NUMBER_WORDS,
    _ORDINALS,
    _PERIOD_SHIFT_TRIGGERS,
    _QUANTITY_AFTER_RE,
    _TERM_SYNONYMS,
    _TIME_UNITS,
    _TO_DATE_OR_ROLLING_RE,
    _canonical_measure,
    _canonical_metric,
    _explicit_grain,
    _fiscal_calendar,
    _name_matches,
    _named_metric,
    _names_time_axis,
    _object_by_id,
    _object_text,
    _requested_grouping_spans,
    _said_name,
    _shared_subjects,
    _singular,
    _tied_top,
    _time_window,
    _tokens,
)
from .generators import _target_focus_text
from .intent_ir import IntentIR
from .time_reference import time_policy_context, time_timezone
from .visibility import visible_dimensions, visible_object_ids, visible_value_domains


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
    r"(?:the\s+)?(?:last|prior|previous)\s+(?:fiscal\s+)?(?:day|week|month|quarter|year|period)\b",
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
# A population hold whose every governor is hidden from the caller names none of them.
HIDDEN_GOVERNOR = (
    "A definition you can't see governs this measure, so it can't be answered as a raw number."
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
_SUBJECT_CONJUNCTION_RE = re.compile(
    r"\s+(?:and|plus|as\s+well\s+as|along\s+with|together\s+with)\s+|\s*,\s*", re.IGNORECASE
)
# The preposition that opens a time clause, cut off with the clause.
_TIME_LEAD_RE = re.compile(
    r"\s+(?:in|for|during|from|between|on|over|of)(?:\s+the)?\s*$", re.IGNORECASE
)
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
# which alone never downgrades a plan, covers it; catalog-name words do downgrade it.
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
    reported, subjects = (named[0].id if named else ""), [term.id for term in intent_ir.subjects]
    caller = partial_query or {}
    gaps.extend(_governed_metric_gaps(runtime._config, question, query, caller, reported, subjects))
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

    role_window_why = _role_window_why(runtime, text, query)
    if role_window_why is not None:
        return role_window_why
    caller_time = (partial_query or {}).get("time")
    if isinstance(caller_time, dict) and any(
        caller_time.get(key) for key in ("start", "end", "range")
    ):
        # The window must agree in the planning zone. Existing holds read it in UTC, and the
        # planning zone must not admit an explicit interval they held.
        gaps.extend(
            _caller_window_gaps(runtime, text, query)
            or _caller_window_gaps(runtime, text, query, timezone="UTC")
        )
    else:
        gaps.extend(_time_window_gaps(runtime, text, query))
    gaps.extend(_fiscal_calendar_gaps(runtime._config, text, query))
    gaps.extend(_subject_window_gaps(runtime._config, query))
    if not _time_window(question, policy_context=query.get("policy_context")).as_of:
        # As-of cues already hold as TIME_WINDOW_UNRESOLVED, without a query.
        gaps.extend(_stock_as_of_gaps(runtime._config, query))
    gaps.extend(_ranking_gaps(runtime, text, query))
    gaps.extend(_ambiguous_grouping_gaps(text, query, partial_query or {}))
    gaps.extend(_where_clause_gaps(runtime, text, query))
    contradictions = _contradictory_filter_gaps(query)
    if contradictions:
        # No row can satisfy the draft. Report that decisive failure once;
        # value-specific absences are consequences of the same contradiction.
        gaps.extend(contradictions)
    else:
        gaps.extend(_filter_value_gaps(runtime, text, query))

    return _coverage_why(gaps)


def _ambiguous_grouping_gaps(
    text: str, query: dict[str, Any], partial_query: dict[str, Any]
) -> list[CoverageGap]:
    """Refuse when the draft adds a grouping beside the caller's ``group_by``.

    Whether the question's grouping phrase restates a caller dimension or asks
    for another one is not decided by matching names: the caller confirms by
    passing every intended dimension ID in ``group_by``.
    """

    from .generators import _requested_grouping_terms  # noqa: WPS433

    authored = set(partial_query.get("group_by") or [])
    added = set(query.get("group_by") or []) - authored
    if not authored or not added:
        return []
    return [
        CoverageGap(
            kind="ambiguous_grouping",
            clause=", ".join(_requested_grouping_terms(text)) or text,
            message="The draft adds a grouping dimension the caller's group_by does not include.",
            actual={"dimension_ids": sorted(authored | added)},
            recovery_hint={
                "kind": "clarify_grouping",
                "message": "Pass every intended grouping dimension ID in group_by.",
            },
        )
    ]


def _governed_metric_gaps(
    config: Any,
    question: str,
    query: dict[str, Any],
    partial_query: dict[str, Any],
    reported: str,
    subjects: list[str],
) -> list[CoverageGap]:
    """Hold a draft that answers with a measure a metric filters, where that metric fits.

    The draft selects the measure itself, or the metric that is its plain aggregate, and does
    not select a metric that aggregates the measure through a filter while the question's
    whole question names that metric, or the measure is a building block. Otherwise a visible
    metric that narrows its rows holds it (``_population_hold``). A measure or metric
    the caller's ``partial_query`` names is the caller's choice; ``reported`` already has its
    own gap.
    """

    caller = set(_referenced_ids(partial_query))
    selected = list(
        dict.fromkeys(
            node[key]
            for node in _dict_nodes(list(query.get("select") or []))
            for key in ("measure", "metric")
            if isinstance(node.get(key), str)
        )
    )
    # Governance is enforcement: it reads the whole package, not the caller's view.
    building_blocks = building_block_measures(base_of(config))
    gaps: list[CoverageGap] = []
    for object_id in selected:
        plain = _object_by_id(config.metric_recipes, object_id)
        measure_id = published_measure(plain) if plain is not None else object_id
        if not measure_id or {object_id, measure_id} & caller:
            continue
        governing = governing_metrics(config, measure_id)
        visible = set(visible_object_ids(config, (metric.id for metric in governing)))
        governing = [metric for metric in governing if metric.id in visible]
        metrics = [
            metric.id
            for metric in governing
            if metric.id not in selected
            and metric.id != reported
            and (measure_id in building_blocks or _said_name(metric, question))
        ]
        expected: dict[str, Any] | None = {"metrics": metrics}
        if not metrics and (measure_id not in building_blocks or governing):
            expected = _population_hold(config, measure_id, query, [*selected, reported], subjects)
        if expected is None:
            continue
        measure = _object_by_id(config.measures, measure_id)
        gaps.append(
            CoverageGap(
                kind="governed_metric_unrealized",
                clause=str(getattr(measure, "label", "") or measure_id),
                message=(
                    "The draft reads this measure without the filter of a governed metric "
                    "that fits the question."
                    if metrics
                    else "The package's governed metrics leave out some of this measure's rows, "
                    "and the draft counts all of them."
                    if expected.get("metrics")
                    else HIDDEN_GOVERNOR
                    if "narrowed_by" in expected
                    else "The draft reads a building-block measure without a visible governed metric."
                ),
                expected=expected,
                actual={"measure": measure_id},
                recovery_hint={
                    "kind": "use_governed_metric",
                    "message": (
                        "Select the governed metric in Query IR. Pass the measure in "
                        "partial_query only when the question asks for every row it counts."
                    ),
                },
            )
        )
    return gaps


def _population_hold(
    config: Any, measure_id: str, query: dict[str, Any], skipped: list[str], subjects: list[str]
) -> dict[str, Any] | None:
    """The gap's ``expected`` for metrics narrowing the measure's rows that the draft neither
    selects (``skipped``) nor filters or groups by, else ``None``. Fails closed.

    Governors come from the whole package; a metric hidden in its own right governs nothing for
    this caller, while one hidden through what it reads still holds, naming only what they see.
    """

    filters = {key: query.get(key) for key in ("where", "group_by", "metric_filters")}
    drafted = set(_referenced_ids(filters))
    try:
        governors = population_governors(base_of(config), measure_id)
    except Exception:  # noqa: BLE001 — an unreadable metric cannot make a draft ready
        return {"metrics": []}
    found = {
        row.id: dims
        for row, dims in governors
        if row.id not in skipped and not hidden_on_its_own(config, row.id) and not dims & drafted
    }
    if not found:
        return None
    shown = {row.id for row in config.metric_recipes}
    metrics = [*dict.fromkeys([*(key for key in subjects if key in found), *sorted(found)])]
    narrowed_by = {row.id for row in config.dimensions} & set().union(*found.values())
    return {
        "metrics": [key for key in metrics if key in shown][:5],
        "narrowed_by": sorted(narrowed_by),
    }


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


def named_subject_why(
    runtime: Any, question: str, partial_query: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """A shared whole name cannot be settled by the ranking's label or score."""

    rows = _shared_subjects(runtime._config, question)
    if not rows or any(row.id in _projected_subject_ids(partial_query or {}) for row in rows):
        return None
    return _coverage_why(
        [
            CoverageGap(
                kind="subject_ambiguous",
                clause=_target_focus_text(question),
                message="The question names more than one selectable subject.",
                expected={"candidates": [row.id for row in rows], "candidate_count": len(rows)},
                actual={},
                recovery_hint={
                    "kind": "name_one_subject",
                    "message": "Ask again naming the one you mean: "
                    + " or ".join(f"{row.label} ({row.id})" for row in rows)
                    + ".",
                },
            )
        ]
    )


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
    collision = named_subject_why(runtime, text, partial_query)
    if collision is not None:
        return collision
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


def _is_prior_period_offset(
    runtime: Any, query: dict[str, Any], windows: list[dict[str, Any]]
) -> bool:
    """Whether the question's one window is the offset of a prior-period comparison.

    "alongside the previous month's revenue" names the comparison's offset, not a window, when
    the draft carries one. Both window checks (the window plan reads, and the caller's) share it.
    """

    if len(windows) != 1:
        return False
    last = (windows[0].get("range") or {}).get("last") or {}
    return (
        set(windows[0]) == {"range"}
        and last.get("value") == 1
        and _query_contains_prior_period(runtime, query)
    )


def _time_window_gaps(runtime: Any, text: str, query: dict[str, Any]) -> list[CoverageGap]:
    """The draft doesn't carry the window the question names, or carries another one."""

    expected = _time_window(text, policy_context=query.get("policy_context")).bounds
    if not expected:
        return []
    if _is_prior_period_offset(runtime, query, [expected]):
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


# A span a label, name or question states: "(14 days)", "14-day", "_14d", "4 weeks", "1 year";
# not the upper end of a range such as "31-60 days".
_SPAN_RE = re.compile(
    r"(?<![\w.\-\u2013])(\d+)\s*-?\s*(d|days?|w|wks?|weeks?|mo|months?|q|quarters?|y|yrs?|years?)\b"
)
_SPAN_UNIT_DAYS = {"d": 1, "w": 7, "m": 30, "q": 91, "y": 365}
_GRAIN_DAYS = {"day": 1, "week": 7, "month": 30, "quarter": 91, "year": 365}


def _stated_spans(text: str) -> set[int]:
    """Every span, in days, that ``text`` states (identifiers read with spaces for ``_``)."""
    lowered = str(text or "").lower().replace("_", " ")
    return {int(n) * _SPAN_UNIT_DAYS[unit[0]] for n, unit in _SPAN_RE.findall(lowered)}


def _same_span(a: int, b: int) -> bool:
    # A month is 28-31 days, a year 360-366.
    return a == b or (a >= 28 and b >= 28 and abs(a - b) <= max(3, max(a, b) // 50))


def _draft_span_days(query: dict[str, Any]) -> int:
    """Days in the period the draft reports each value for: its bucket, else its window."""
    time = _time_block(query)
    if str(time.get("grain") or "") in _GRAIN_DAYS:
        return _GRAIN_DAYS[str(time["grain"])]
    try:
        if time.get("start") and time.get("end"):
            start, end = (date.fromisoformat(str(time[key])[:10]) for key in ("start", "end"))
            return (end - start).days
    except ValueError:
        pass
    last = dict(dict(time.get("range") or {}).get("last") or {})
    if last.get("unit") in _GRAIN_DAYS:
        return _GRAIN_DAYS[last["unit"]] * int(last.get("value") or 1)
    return 0


def _subject_window_gaps(config: Any, query: dict[str, Any]) -> list[CoverageGap]:
    """The subject is a stock over its own trailing window, and each row reports another period.

    "Unique visitors (14 days)" filtered to this week is the 14-day count as of the week's
    last snapshot, not this week's unique visitors. The window is read from the stock's
    label, name or id. Nothing the question says turns the check off: a false match only
    lowers confidence, while a missed one returns a wrong number as the period's.
    """
    asked = _draft_span_days(query)
    if not asked:
        return []
    objects = {row.id: row for row in [*config.measures, *config.metric_recipes]}
    gaps: list[CoverageGap] = []
    for subject_id in _projected_subject_ids(query):
        row = objects.get(subject_id)
        kind = getattr(row, "measure_class", "") or getattr(row, "kind", "")
        if kind != "semi_additive":
            continue
        label = str(getattr(row, "label", "") or subject_id)
        spans = _stated_spans(f"{label} {getattr(row, 'name', '')} {subject_id}")
        if len(spans) != 1:
            continue
        (own,) = spans
        if _same_span(own, asked):
            continue
        gaps.append(
            CoverageGap(
                kind="subject_window_mismatch",
                clause=label,
                message=(
                    f"{label} is a value over its own {own}-day window as of each point in time, "
                    f"not a total for the {asked}-day period the question asks about."
                ),
                expected={"period_days": asked},
                actual={"subject": subject_id, "subject_window_days": own},
                recovery_hint={
                    "kind": "ask_within_the_subject_window",
                    "message": (
                        f"Ask for {label} as of a date (it covers the {own} days before it), or "
                        "choose a measure that covers the period you asked about."
                    ),
                },
            )
        )
    return gaps


def _stock_as_of_gaps(config: Any, query: dict[str, Any]) -> list[CoverageGap]:
    """Hold predicate stocks at any grain, and direct stocks without one as-of day."""
    grain = _time_block(query).get("grain") or None
    try:
        stocks = _multi_series_stocks(config, query)
    except Exception:  # noqa: BLE001 — an unreadable stock cannot make a draft ready
        stocks = None
    if stocks is not None and grain == "day":
        stocks = {stock: True for stock, predicate in stocks.items() if predicate}
    if stocks == {}:
        return []
    shown = visible_object_ids(config, stocks or [])
    period = f"each {grain}" if grain else "the whole history (no time block)"
    selected = (
        visible_object_ids(config, _projected_subject_ids(query)) if stocks is not None else []
    )
    recipes = {row.id: row for row in config.metric_recipes} if selected else {}
    metric = next((recipes[subject] for subject in selected if subject in recipes), None)
    label = str(getattr(metric, "label", "") or "")
    if not label and shown:
        label = str(getattr(_object_by_id(config.measures, shown[0]), "label", "") or "")
    label = label or "the balance"
    predicate_stock = any((stocks or {}).values())
    return [
        CoverageGap(
            kind="stock_as_of_unrealized",
            clause=", ".join(shown) or "stock",
            message=(
                "A balance read through a predicate may have its own time scope; "
                "the outer grain does not prove a one-day read."
                if predicate_stock
                else f"Read a balance on one day; this draft adds each series' last value over {period}."
            ),
            expected={"grain": "day", "stocks": shown},
            actual={"grain": grain},
            recovery_hint={
                "kind": "ask_for_one_day",
                "message": (
                    "Choose a metric without a stock predicate, or select the balance directly."
                    if predicate_stock
                    else f"Ask for '{label} yesterday' or '{label} on <YYYY-MM-DD>', "
                    "or set time.grain: day with that day's start and end."
                ),
            },
        )
    ]


def _multi_series_stocks(config: Any, query: dict[str, Any]) -> dict[str, bool]:
    """Map each stock to whether any path to it crosses a predicate, including recipes."""
    measures = {row.id: row for row in config.measures}
    recipes = {row.id: row for row in config.metric_recipes}
    pending: list[tuple[Any, bool]] = [
        (query.get(key) or [], False) for key in ("select", "metric_filters", "where")
    ]
    seen: set[tuple[str, bool]] = set()
    stocks: dict[str, bool] = {}
    while pending:
        node, predicate = pending.pop()
        if isinstance(node, list):
            pending.extend((child, predicate) for child in node)
            continue
        if not isinstance(node, dict):
            continue
        predicate = predicate or node.get("kind") == "metric_predicate"
        pending.extend(
            (child, predicate or (node.get("kind") == "scoped_aggregate" and key == "predicates"))
            for key, child in node.items()
            if isinstance(child, (dict, list))
        )
        for object_id in (node.get(key) for key in ("measure", "metric", "metric_recipe")):
            if not isinstance(object_id, str) or (object_id, predicate) in seen:
                continue
            seen.add((object_id, predicate))
            if (recipe := recipes.get(object_id)) is not None:
                pending.append((expr_to_dict(recipe.expression), predicate))
            elif (
                row := measures.get(object_id)
            ) is not None and row.measure_class == "semi_additive":
                clock = row.default_temporal_role or next(
                    iter(row.compatible_temporal_roles or []), ""
                )
                if _snapshot_series_columns(row, clock, config):
                    stocks[object_id] = stocks.get(object_id, False) or predicate
    return stocks


def _fiscal_calendar_gaps(config: Any, text: str, query: dict[str, Any]) -> list[CoverageGap]:
    """The question counts time in fiscal periods, but the draft counts Gregorian ones.

    A draft honors a fiscal bucket ("by fiscal quarter") by bucketing on a
    non-default calendar (the planner picks only the fiscal one; a caller may name
    another), or by grouping on a dimension whose name says fiscal (a fiscal-period
    column on the fact). A question with no fiscal bucket is honored on days, too.
    Any other fiscal mention ("the first fiscal quarter") also needs the period as
    exact days in the draft's window. A draft with no time buckets honors it by
    filtering on such a dimension. Nothing honors a to-date or rolling value:
    period-to-date resets on Gregorian periods.
    """

    lowered = text.lower()
    fiscal = _FISCAL_RE.search(lowered)
    if fiscal is None:
        return []
    time = _time_block(query)
    rolling = _TO_DATE_OR_ROLLING_RE.search(lowered) is not None
    scoped = not _FISCAL_RE.search(_FISCAL_BUCKET_RE.sub(" ", lowered)) or any(
        time.get(key) for key in ("start", "end", "range")
    )
    named = {
        row.id
        for row in visible_dimensions(config)
        if "fiscal" in _tokens(f"{row.id} {row.name} {row.label}")
    }
    bucketed = (
        str(time.get("calendar_id") or "default").lower() != "default"
        # A day is a day on any calendar, unless the question asks for fiscal buckets.
        or (time.get("grain") == "day" and not _FISCAL_BUCKET_RE.search(lowered))
        or bool(named & {str(item) for item in query.get("group_by") or []})
    )
    filtered = not time.get("grain") and bool(named & set(_referenced_ids(query)))
    if not rolling and ((scoped and bucketed) or filtered):
        return []
    calendar = _fiscal_calendar(config)
    calendars = sorted(
        {row.calendar_id for row in config.entities if row.kind == "time" and row.calendar_id}
    )
    steps = []
    if rolling:
        steps.append(
            "ask for the fiscal buckets alone: plan can't draft a fiscal to-date or rolling value"
        )
    elif not bucketed and calendar is not None:
        steps.append(
            f"set query.time.calendar_id to {calendar.calendar_id!r} and time.fill to true"
            + ("" if time.get("grain") else " with a temporal_role and grain")
        )
    if not scoped:
        steps.append(
            "give any fiscal period as exact query.time.start and end dates (or ask only for "
            "fiscal buckets, as in 'by fiscal quarter')"
        )
    hint = " and ".join(steps)
    return [
        CoverageGap(
            kind="fiscal_calendar_unrealized",
            clause=fiscal.group(0),
            message=(
                "The question asks for a to-date or rolling value in fiscal periods."
                if rolling
                else "The question names a fiscal period, but the draft carries no window for it."
                if bucketed
                else "The question counts time in fiscal periods, but the draft buckets and "
                "bounds time on the Gregorian calendar."
            ),
            expected={"calendar_id": calendar.calendar_id if calendar else "fiscal"},
            actual={"calendar_id": time.get("calendar_id") or "default"},
            recovery_hint={
                "kind": "use_fiscal_calendar",
                "message": (
                    f"{hint[:1].upper()}{hint[1:]}, then validate."
                    if calendar
                    else "plan found no single calendar named fiscal in this package"
                    + (f" (its calendars: {', '.join(calendars)})" if calendars else "")
                    + ": set query.time.calendar_id to the one you mean with time.fill true, "
                    "author a fiscal calendar, or ask in calendar-year terms."
                ),
            },
        )
    ]


def _ranking_request(text: str, nouns: frozenset[str] = frozenset()) -> dict[str, Any] | None:
    """Parse a ranking request into (clause, limit, direction, noun, requires_order, count_at).

    ``limit`` is None when the question fixes no count ("the top products"),
    and ``direction`` is None when it fixes no order ("rank stores by
    revenue"). A bare superlative ranks the noun after it only when that noun
    is a time unit ("the highest revenue month"), follows a hyphenated
    superlative ("best-selling products"), or follows "best"/"worst" and is
    one of the catalog's dimension ``nouns`` ("the best store"): in "the
    highest revenue", revenue is what is measured, not what is ranked.
    """

    words = [word for word, _start, _end in _ranking_words(text)]
    for index in range(len(words)):
        request = _ranking_at(words, index, nouns)
        if request is not None:
            return request
    return None


def _ranking_words(text: str) -> list[tuple[str, int, int]]:
    """The words of a ranking question with their character spans in its lowercase form."""

    # "top-3 stores" is "top 3 stores" (the same length, so spans still index the question).
    lowered = re.sub(r"\b(top|bottom|best|worst)-(\d+)\b", r"\1 \2", str(text or "").lower())
    return [(match.group(0), *match.span()) for match in _WORD_RE.finditer(lowered)]


def _ranking_count_spans(text: str, limit: int, nouns: frozenset[str]) -> list[tuple[int, int]]:
    """Where the question states ``limit`` as the count of a ranking ("top 5 stores", "the 5
    customers who spent the most", "3 stores with the least revenue", "which 2 stores ...")."""

    scanned = _ranking_words(text)
    words = [word for word, _start, _end in scanned]
    spans: list[tuple[int, int]] = []
    for index in range(len(words)):
        request = _ranking_at(words, index, nouns)
        if request is not None and request["limit"] == limit and request["count_at"] is not None:
            _word, start, end = scanned[request["count_at"]]
            spans.append((start, end))
    return spans


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
        at = index + 1 if count is not None else None
        return _ranking(words, index, end, limit, direction, noun, False, at)
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
        return _ranking(words, start, end, count, direction, noun, False, index)
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
        at = cursor if count is not None else None
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
        return _ranking(words, index, end, limit, direction, noun, False, at)
    superlative = _superlative(words, index)
    if superlative and _count(words, index + 1, years=True) is not None:
        # "best 3 stores by revenue", "highest 5 products"
        noun, end = _noun_phrase(words, index + 2)
        if noun:
            count = _count(words, index + 1, years=True)
            return _ranking(words, index, end, count, superlative, noun, False, index + 1)
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
    count_at: int | None = None,
) -> dict[str, Any]:
    return {
        "clause": " ".join(words[start:end]),
        "limit": limit,
        "direction": direction,
        "noun": noun,
        "requires_order": requires_order,
        # The index of the word that states the count, when the question states one.
        "count_at": count_at,
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
            for row in visible_dimensions(config)
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
        _singular(token) for row in visible_dimensions(config) for token in _tokens(_core_text(row))
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
            for row in visible_dimensions(runtime._config)
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
    for domain in visible_value_domains(config):
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
        for row in visible_dimensions(config)
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

    rows = visible_dimensions(config)
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
        if is_child_group(node):
            # A child group's conditions cut child rows, a scope of its own: unproven here.
            for condition in list(node.get("where") or []):
                if isinstance(condition, dict) and isinstance(condition.get("field"), str):
                    out.setdefault(condition["field"], _FieldConstraints()).uncertain = True
            continue
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
        *visible_dimensions(config),
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
        if is_child_group(node) and node.get("match") == "none":
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
    # The time clause is no part of the last subject: "revenue and orders in Q1 2017".
    for start, end in sorted(_time_window(target_text).spans, reverse=True):
        target_text = _TIME_LEAD_RE.sub("", target_text[:start]) + target_text[end:]
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
    label = str(getattr(row, "label", "") or "")
    fields = (
        object_id,
        id_suffix,
        str(getattr(row, "name", "") or ""),
        label,
        # "item revenue" names "Item revenue (USD)".
        re.sub(r"\s*\(.*?\)", "", label),
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


# Words that state a clock time or a zone. Like a numeral, one no part of the draft consumes
# means the answer may differ from the question (see unconsumed_terms). "second" alone is an
# ordinal ("second order"); a count of seconds is caught as a numeral. "min", "hr" and "am"
# are left out: they only carry meaning beside a numeral, and the numeral is caught.
_CLOCK_WORDS = frozenset(
    {"seconds", "minute", "minutes", "hour", "hours", "hourly", "clock"}
    | {"noon", "midnight", "midday", "morning", "afternoon", "evening", "overnight", "tonight"}
    | {"utc", "gmt", "zulu", "tz", "timezone", "timezones"}
)
# Spelled-out numbers: "from nine to five", "at twelve", "half past two".
_SPOKEN_NUMBERS: dict[str, float] = {
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "six": 6.0,
    "seven": 7.0,
    "eight": 8.0,
    "nine": 9.0,
    "ten": 10.0,
    "eleven": 11.0,
    "twelve": 12.0,
    "thirteen": 13.0,
    "fourteen": 14.0,
    "fifteen": 15.0,
    "sixteen": 16.0,
    "seventeen": 17.0,
    "eighteen": 18.0,
    "nineteen": 19.0,
    "twenty": 20.0,
    "thirty": 30.0,
    "forty": 40.0,
    "fifty": 50.0,
    "sixty": 60.0,
    "seventy": 70.0,
    "eighty": 80.0,
    "ninety": 90.0,
    "hundred": 100.0,
    "half": 0.5,
}
# "quarter" is a period ("last quarter"); only "quarter past" and "quarter to" tell a time.
_QUARTER_CLOCK_RE = re.compile(r"\bquarter\s+(?:past|to|after|till|until|before)\b")
# The zone codes a question can carry; docs/MCP_INTERFACE.md lists them. Codes that are also
# words ("cat", "eat", "west") never count by their lowercase spelling.
_ZONE_CODES = frozenset(
    {"est", "edt", "cst", "cdt", "mst", "mdt", "pst", "pdt", "akst", "akdt", "hst", "cet", "cest"}
    | {"eet", "eest", "bst", "ist", "jst", "kst", "msk", "aest", "aedt", "acst", "acdt", "awst"}
    | {"nzst", "nzdt", "sgt", "hkt", "wib", "sast", "pkt"}
)
# Short codes count only in capitals, in a question that isn't all capitals ("in ET",
# "12:00 PT", "12:00 Z"; "Z" is Zulu). Words that are also zone codes ("west", "cat") never do.
_CASED_ZONE_CODES = frozenset({"Z", "ET", "PT", "CT", "MT"})
_ZONE_NAME_RE = re.compile(
    r"\b(?:africa|america|antarctica|arctic|asia|atlantic|australia|europe|indian|pacific|etc)"
    r"/[a-z_]+",
    re.IGNORECASE,
)
_TERM_RE = re.compile(r"\d+(?:[.,]\d+)+(?![^\W_])|[^\W_]+")
_YEAR_WORD_RE = re.compile(r"(?:19|20)\d{2}")


def _number_key(value: float) -> str | None:
    """A number as digits, or ``None`` for one too large to read (a caller's 10**400)."""

    try:
        number = float(value)
        return str(int(number)) if number.is_integer() else repr(number)
    except (OverflowError, ValueError):
        return None


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


def unconsumed_terms(runtime: Any, question: str, query: dict[str, Any]) -> list[str]:
    """The numerals, number words and clock or zone words no part of the draft consumes.

    The invariant plan holds a draft to before it calls it ready: every number ("9", "14h30",
    "nine"), clock word ("hour", "noon", "o'clock") and zone ("UTC", "EST", "Z",
    "Europe/Berlin") in the question sits inside the character span of a construct the draft
    carries: a date or window, the limit, threshold or percentile it states, a filter value, or
    the name of an object it selects. Consumption is by span, never by value alone: a "1930"
    that no window phrase or threshold's own number holds is left over, and so is any number
    no construct reads. One left over is an hour, a range or a threshold the draft silently
    dropped.
    """

    text = str(question or "")
    lowered = text.lower()
    tokens = [(match.group(0), *match.span()) for match in _TERM_RE.finditer(lowered)]
    terms = _time_and_number_terms(text, lowered, tokens)
    # "Europe/Berlin" reads as two plain words, so it is one term, named whole.
    terms.extend((zone.group(0), *zone.span()) for zone in _ZONE_NAME_RE.finditer(lowered))
    if not terms:
        return []
    consumed = _consumed_spans(runtime, lowered, tokens, query)
    out: list[str] = []
    for word, start, end in sorted(terms, key=lambda term: term[1]):
        inside = any(low <= start and end <= high for low, high in consumed)
        if not inside and word not in out:
            out.append(word)
    return out


def _time_and_number_terms(
    text: str, lowered: str, tokens: list[tuple[str, int, int]]
) -> list[tuple[str, int, int]]:
    """Every numeral, number word, clock word and zone code of the question, with its span."""

    # Short codes are read in the question's own case; the lengths must agree to slice by span.
    cased = len(lowered) == len(text)
    mixed = not cased or text != text.upper()
    quarters = {match.start() for match in _QUARTER_CLOCK_RE.finditer(lowered)}
    out: list[tuple[str, int, int]] = []
    for word, start, end in tokens:
        original = text[start:end] if cased else word.upper()
        if (
            any(char.isdigit() for char in word)
            or word in _SPOKEN_NUMBERS
            or word in _CLOCK_WORDS
            or word in _ZONE_CODES
            or start in quarters
            or (mixed and original in _CASED_ZONE_CODES)
        ):
            out.append((word, start, end))
    return out


def _consumed_spans(
    runtime: Any, lowered: str, tokens: list[tuple[str, int, int]], query: dict[str, Any]
) -> list[tuple[int, int]]:
    """The character spans of the question the draft's constructs consume.

    A window consumes the date phrases the planner resolved when the draft carries one (both
    bounds, or a range) that agrees with them (``_window_agrees``), and the phrases it could
    not resolve; never a clock time or a bare year. A limit
    consumes the count of the ranking that states it ("top 5", "the 5 customers who spent the
    most"); a threshold, percentile or numeric filter value consumes its own number token, found
    where the question states it ("over 12.50", "90th percentile", "1,000 or more", "size 12").
    A filter value, or the name of an object the draft selects, consumes the tokens that spell it.
    """

    spans: list[tuple[int, int]] = []
    time = _time_block(query)
    if any(time.get(key) for key in ("start", "end", "range")):
        spans.extend(_window_spans(lowered, time, query.get("policy_context")))
    normal = [_singular(_TERM_SYNONYMS.get(word, word)) for word, _start, _end in tokens]
    referenced = set(_referenced_ids(query))
    calendar_id = str(time.get("calendar_id") or "default")
    rows = _catalog_rows(runtime._config)
    for row in rows:
        if str(getattr(row, "id", "")) in referenced or (
            calendar_id != "default" and getattr(row, "calendar_id", "") == calendar_id
        ):
            # An object's own names only: a description that says "per hour" consumes nothing.
            names = [str(getattr(row, attr, "") or "") for attr in ("id", "name", "label")]
            spans.extend(_name_spans(tokens, normal, names, whole=False))
            spans.extend((start, end) for _, start, end in _name_matches(row, lowered))
    labels = _value_phrases(runtime._config)
    fields = {str(getattr(row, "id", "")): row for row in rows}
    for node in _dict_nodes(query):
        if "field" not in node or "value" not in node:
            continue
        value = node.get("value")
        items = value if isinstance(value, list) else [value]
        names = [str(item) for item in items if not _is_number(item)]
        for item in list(names):
            # A filter on 'jaffle' consumes the question's "food".
            for _domain, row in labels.get(_plain(item), []):
                names.extend(_value_names(row))
        spans.extend(_name_spans(tokens, normal, names, whole=True))
        row = fields.get(str(node["field"]))
        if row is not None:
            # A number filter consumes the number token beside its field's name ("size 12").
            field_words = {
                word
                for attr in ("id", "name", "label")
                for word in _normal_tokens(str(getattr(row, attr, "") or ""))
            }
            spans.extend(
                _number_spans(
                    lowered, tokens, *_draft_numbers({"value": items}), normal, field_words
                )
            )
    spans.extend(_number_spans(lowered, tokens, *_draft_numbers(query)))
    limit = query.get("limit")
    limit = _number_key(limit) if isinstance(limit, (int, float)) else None
    if limit is not None and limit.isdigit():
        # A ranking's count is read where the ranking parser reads it, whatever words sit beside
        # it. A limit the draft carries and the question doesn't state stays unconsumed.
        nouns = _dimension_nouns(runtime._config)
        spans.extend(_ranking_count_spans(lowered, int(limit), nouns))
    return spans


def _normal_tokens(text: str) -> tuple[str, ...]:
    return tuple(_singular(token) for token in _tokens(text))


def _name_spans(
    tokens: list[tuple[str, int, int]], normal: list[str], names: list[str], whole: bool
) -> list[tuple[int, int]]:
    """Where the question spells a name: all of it (a filter value), or any run of its words (an
    object's name: "by hour" for "Hour of day")."""

    spans: list[tuple[int, int]] = []
    for name in names:
        phrase = _normal_tokens(name)
        if not phrase:
            continue
        for first in range(len(normal)):
            for offset in range(len(phrase)):
                if whole and offset:
                    break
                length = 0
                while (
                    first + length < len(normal)
                    and offset + length < len(phrase)
                    and normal[first + length] == phrase[offset + length]
                ):
                    length += 1
                if length and (not whole or length == len(phrase)):
                    spans.append((tokens[first][1], tokens[first + length - 1][2]))
    return spans


# Where a question states a threshold or a percentile: a comparison before its number ("over
# 12.50", "at least 2") or after it ("1,000 or more", "5+"). Never a ranking's cue ("top",
# "first", "10 largest"): a ranking's count is the limit's, read by the ranking parser, so a
# threshold cannot consume it and the limit cannot consume a threshold.
_NUMBER_BEFORE_RE = re.compile(
    r"(?:\b(?:more\s+than|greater\s+than|over|above|exceeds?|exceeded|exceeding|at\s+least"
    r"|no\s+fewer\s+than|minimum\s+of|less\s+than|fewer\s+than|under|below|at\s+most"
    r"|no\s+more\s+than|maximum\s+of|equals?|equal\s+to)|[<>=]=?)[\s-]*[$£€]?$"
)
_NUMBER_AFTER_RE = re.compile(
    r"^\s*(?:\+|or\s+(?:more|less|fewer|greater|higher|lower|above|below|over|under)\b)"
)
# A number is a percentage only when the question says so: "50 %", "50 percent", "90th percentile".
_PERCENT_AFTER_RE = re.compile(r"^\s*(?:%|percent\b|pct\b|(?:st|nd|rd|th)?\s*percentile\b)")


def _token_value(word: str) -> float | None:
    """The number a token states: "12.50" is 12.5, "90th" is 90, "five" is 5."""

    plain = re.fullmatch(r"(\d+(?:[.,]\d+)*)(?:st|nd|rd|th)?", word)
    try:
        return float(plain.group(1).replace(",", "")) if plain else _SPOKEN_NUMBERS.get(word)
    except ValueError:
        return None


def _number_spans(
    lowered: str,
    tokens: list[tuple[str, int, int]],
    numbers: set[str],
    percents: set[str],
    normal: list[str] | None = None,
    near: set[str] | None = None,
) -> list[tuple[int, int]]:
    """The number tokens that state one of the draft's numbers where a construct states it: a
    threshold or percentile cue beside it, or (with ``near``) a filter's field name.

    A token is a percentage only with a percent cue after it: "50 percent" states 0.5 (and
    "10 percent" the 0.9 of a "top 10 percent" cut), while a bare "50" or "500" never does.
    """

    spans: list[tuple[int, int]] = []
    for index, (word, start, end) in enumerate(tokens):
        value = _token_value(word)
        if value is None:
            continue
        percent = _PERCENT_AFTER_RE.match(lowered[end : end + 60]) is not None
        key = _number_key(value)
        if not (
            key in numbers or (percent and (key in percents or _number_key(value / 100) in numbers))
        ):
            continue
        if near is not None and normal is not None:
            stated = bool(near & set(normal[max(index - 1, 0) : index + 2]))
        else:
            stated = (
                percent
                or _NUMBER_BEFORE_RE.search(lowered[max(start - 40, 0) : start]) is not None
                or _NUMBER_AFTER_RE.match(lowered[end : end + 60]) is not None
            )
        if stated:
            spans.append((start, end))
    return spans


# A year a date phrase states: after "in", "for" or "during", or the word "year". Only the 2000s,
# as when plan reads a short question ("1930" is never a date there).
_YEAR_CUE_RE = re.compile(r"\b(?:in|for|during|year)\s+(20\d{2})\b")
# The rest of a bound after its date, when it is midnight.
_MIDNIGHT_RE = re.compile(r"(?:[t ]00:00(?::00(?:\.0+)?)?(?:z|[+-]00:?00)?)?")


def _question_time(
    lowered: str,
    policy_context: dict[str, Any] | None = None,
    *,
    timezone: str | None = None,
) -> tuple[list[tuple[tuple[int, int], dict[str, Any]]], list[tuple[int, int]]]:
    """The windows plan reads from the question, and the other spans it reads as time.

    The other spans are time phrases plan cannot resolve ("last 24 hours", "before 2017"). A
    bare year is one only with the bound word plan reports it by ("before 2017"); on its own it
    may be an hour ("at 2000"), so it is never returned.

    A question too long to read has a window only when its date phrases state one calendar year
    ("in 2017", "for 2017"; never "at 2000" or "in 2000+"). Two different years cannot be told
    from a quantity ("in 2017 ... for 2000 customers") without the resolver, so such a question
    states no window and each year in it is left unconsumed.
    """

    if len(lowered) > _MAX_TIME_TEXT:
        years = [
            (match.span(1), int(match.group(1)))
            for match in _YEAR_CUE_RE.finditer(lowered)
            if _QUANTITY_AFTER_RE.match(lowered[match.end(1) :]) is None
        ]
        if len({year for _span, year in years}) > 1:
            return [], []
        return [
            (span, {"start": f"{year:04d}-01-01", "end": f"{year + 1:04d}-01-01"})
            for span, year in years
        ], []
    read = _time_window(lowered, policy_context=policy_context, timezone=timezone)
    windows = list(read.windows)
    others: list[tuple[int, int]] = []
    for low, high in read.spans:
        if any(cue.span == (low, high) for cue in read.as_of):
            # An interval cannot consume a snapshot request.
            continue
        if any(start <= low and high <= end for (start, end), _bounds in windows):
            continue
        if _YEAR_WORD_RE.fullmatch(lowered[low:high].strip()):
            # A bare year is a date only as the phrase plan reports it: after a bound word
            # ("before 2017", "of 2017"), the span starting at that word. "at 2000" is not one.
            bound = _BOUNDARY_BEFORE_RE.search(lowered[:low])
            if bound is None:
                continue
            low = bound.start()
        others.append((low, high))
    return windows, others


def _window_days(
    bounds: dict[str, Any],
    policy_context: dict[str, Any] | None = None,
    *,
    timezone: str | None = None,
) -> tuple[date | None, date | None] | None:
    """The first day a window covers and the first day after it, or None where unreadable.

    A relative range is read in ``timezone``, the planning zone unless one is given. A bound is
    readable only at a whole day, the grain of every window plan reads: a date, or that date at
    midnight. A bound with another time of day is unreadable, as is a window whose start is not
    before its end (empty or reversed); a missing bound comes back None. A bound with a zone
    designator is readable only when its offset is its temporal role's at that instant: then
    its written date is the role-local date.
    """

    role = str(bounds.get("temporal_role") or "")
    if bounds.get("range"):
        try:
            bounds = _relative_range_bounds(
                bounds["range"],
                policy_context=time_policy_context(policy_context),
                timezone=timezone or time_timezone(),
            )
        except (SemanticLayerError, ValueError, OverflowError):
            return None
    days: list[date | None] = []
    for key in ("start", "end"):
        raw = str(bounds.get(key) or "").strip()
        text = raw.lower()
        if not text:
            days.append(None)
            continue
        try:
            day = date.fromisoformat(text[:10])
        except ValueError:
            return None
        tail = text[10:]
        if tail.endswith("z") or "+" in tail or "-" in tail:
            try:
                moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                local = moment.astimezone(ZoneInfo(time_timezone(role))).utcoffset()
            except (ValueError, KeyError, OverflowError):
                return None
            if moment.utcoffset() != local:
                return None
        if not _MIDNIGHT_RE.fullmatch(tail):
            return None
        days.append(day)
    if days[0] is not None and days[1] is not None and days[0] >= days[1]:
        return None
    return days[0], days[1]


def _window_agrees(
    windows: list[tuple[tuple[int, int], dict[str, Any]]],
    time: dict[str, Any],
    policy_context: dict[str, Any] | None = None,
    *,
    timezone: str | None = None,
) -> bool:
    """Whether the draft's window is the one the question's date phrases state.

    The one rule for a window in the draft: it carries both bounds, each read only at a whole
    day (see ``_window_days``), and they are the earliest start and the latest end among the
    windows the question states. A missing bound, or a draft that cannot be read, does not
    agree. A question that states no window agrees with any draft.
    """

    if not windows:
        return True
    carried = _window_days(time, policy_context, timezone=timezone)
    starts: list[date] = []
    ends: list[date] = []
    for _span, bounds in windows:
        asked = _window_days(bounds, policy_context, timezone=timezone)
        if asked is None or asked[0] is None or asked[1] is None:
            return False
        starts.append(asked[0])
        ends.append(asked[1])
    return carried == (min(starts), max(ends))


def _window_spans(
    lowered: str,
    time: dict[str, Any],
    policy_context: dict[str, Any] | None = None,
    *,
    timezone: str | None = None,
) -> list[tuple[int, int]]:
    """The spans of the question a window in the draft's ``query.time`` consumes.

    One rule: the draft's window consumes the date phrases plan resolved only if it agrees with
    them (``_window_agrees``); otherwise it consumes none. It also answers a phrase plan cannot
    resolve ("last 24 hours"). It never consumes a bare year, a time of day, an hour, a bare
    number or a zone, whatever the window's bounds say, so a question that states one is
    refused rather than matched to the window by value.
    """

    windows, others = _question_time(lowered, policy_context, timezone=timezone)
    if not _window_agrees(windows, time, policy_context, timezone=timezone):
        return []
    return [span for span, _bounds in windows] + others


def _caller_window_gaps(
    runtime: Any, text: str, query: dict[str, Any], *, timezone: str | None = None
) -> list[CoverageGap]:
    """The window a caller passed is not the one the question's date phrases state."""

    lowered = text.lower()
    context = query.get("policy_context")
    time = _time_block(query)
    windows, _others = _question_time(lowered, context, timezone=timezone)
    if _window_agrees(windows, time, context, timezone=timezone) or _is_prior_period_offset(
        runtime, query, [bounds for _span, bounds in windows]
    ):
        return []
    return [
        CoverageGap(
            kind="time_window_unrealized",
            clause=", ".join(lowered[low:high].strip() for (low, high), _bounds in windows),
            message="The question names a time window, but the draft's window is a different one.",
            expected={"time": [bounds for _span, bounds in windows]},
            actual={"time": time or None},
            recovery_hint={
                "kind": "provide_time_window",
                "message": (
                    "Pass the window the question states in Query IR time (start inclusive, end "
                    "exclusive), or ask about the window you passed."
                ),
            },
        )
    ]


def _role_window_why(runtime: Any, text: str, query: dict[str, Any]) -> dict[str, Any] | None:
    """Hold a draft whose window reads other days in the zone of a role the query reads it on.

    Plan drafts and checks every window in one planning zone (the package default, else UTC),
    while execution filters it on the query's role and on each leg's own role where the leg
    can't read the query's (``_leaf_time_role``, as binding records it), each in its zone. A
    draft is ``ok`` only when the question's windows, and a relative range the draft carries
    for them, read the same days in every such zone as in the planning zone. If binding can't
    say which roles are read, every role is taken as read, so the draft is held.
    """

    time = _time_block(query)
    role = str(time.get("temporal_role") or "")
    planning = time_timezone(runtime=runtime)
    names = [role, *(row.id for row in runtime._config.temporal_roles)]
    zones = {name: time_timezone(name, runtime=runtime) for name in names if name}
    if set(zones.values()) <= {planning}:
        return None
    context = query.get("policy_context")
    asked = _time_window(text, context, timezone=planning).windows
    if not asked:
        return None
    carried = {"range": time["range"]} if time.get("range") else {}

    def differing(zone: str) -> list[tuple[int, int]]:
        if carried and _window_days(carried, context, timezone=planning) != _window_days(
            carried, context, timezone=zone
        ):
            return [span for span, _bounds in asked]
        try:
            read = _time_window(text, context, timezone=zone).windows
        except (ValueError, OverflowError):
            read = ()
        return [
            span
            for (span, bounds), local in zip(asked, read, strict=False)
            if span != local[0]
            or _window_days(bounds, context, timezone=planning)
            != _window_days(local[1], context, timezone=zone)
        ] + [span for span, _bounds in asked[len(read) :]]

    moved = {zone: spans for zone in set(zones.values()) - {planning} if (spans := differing(zone))}
    if not moved:
        return None
    try:
        bound = bind_query(runtime._config, None, query).temporal_roles.values()
        read_roles = {role}.union(*bound)
    except Exception:  # any failure: every role may be read
        read_roles = set(zones)
    held = {
        name: zones[name]
        for name in sorted(read_roles, key=lambda name: (name != role, name))
        if zones.get(name) in moved
    }
    if not held:
        return None
    zone = next(iter(held.values()))
    differing_spans = sorted({span for held_zone in held.values() for span in moved[held_zone]})
    lowered = text.lower()
    return {
        "code": "TIME_WINDOW_UNRESOLVED",
        "message": (
            "The question's window reads different days in the zone of a temporal role the "
            f"query reads it on ({', '.join(dict.fromkeys(held.values()))}) than in the planning "
            f"zone ({planning}), so plan returns no query: either reading may answer a "
            "different question."
        ),
        "details": {
            "path": "time",
            "temporal_role": next(iter(held)),
            "timezone": zone,
            "temporal_roles": held,
            "planning_timezone": planning,
            "unresolved_phrases": list(
                dict.fromkeys(lowered[low:high].strip() for low, high in differing_spans)
            ),
        },
        "recovery_hints": [
            {
                "kind": "rephrase_time_window",
                "message": (
                    "Name the window's dates as the temporal role's zone reads them (e.g. "
                    "'2017-04-03'), so the window doesn't depend on the zone."
                ),
            }
        ],
    }


def _draft_numbers(query: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Every number the draft carries outside its time block and its limit (a threshold, a
    filter value, a percentile), as digits, and the percentages its fractions state (0.9 is the
    "top 10 percent" it cuts). The limit is read by the ranking that states it, never here: a
    threshold that repeats its number ("top 10 stores with at least 10 orders") is not the limit."""

    return _number_sets(
        {key: value for key, value in query.items() if key not in ("time", "limit")}
    )


def _number_sets(query: dict[str, Any]) -> tuple[set[str], set[str]]:
    """The numbers a query carries as digits, and the percentages its fractions state: 0.5
    states "50" (and the "50" of a "top 50 percent" cut)."""

    plain: set[str] = set()
    percents: set[str] = set()

    def add(value: float) -> None:
        key = _number_key(value)
        if key is None:
            return
        plain.add(key)
        if 0 < value < 1 and float(value * 100).is_integer():
            percents.update({str(int(value * 100)), str(100 - int(value * 100))})

    def walk(value: Any) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            add(value)
        elif isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)?", value.strip()):
            add(float(value))
        elif isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk({key: value for key, value in query.items() if key != "version"})
    return plain, percents


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


__all__ = [
    "CoverageGap",
    "intent_faithfulness_why",
    "unconsumed_catalog_words",
    "unconsumed_terms",
    "unconsumed_unknown_words",
    "unmatched_intent_terms",
]
