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
from typing import Any

from ._base import _tokens
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
    for recipe in getattr(runtime.config, "metric_recipes", []) or []:
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
        *getattr(runtime.config, "measures", []),
        *getattr(runtime.config, "metric_recipes", []),
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


__all__ = ["CoverageGap", "intent_faithfulness_why"]
