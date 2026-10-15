"""Coverage gaps, and the query and text readers every faithfulness check shares."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from .call import Call, Time
from .visibility import visible_dimensions, visible_value_domains


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


# Calendar vocabulary ignored by unmatched-word diagnostics, shared with balance readiness.
_TIME_FRAMING_WORDS = frozenset(
    {
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
    }
)

# Words and phrases asking for a comparison ("compared", "vs", "up or down").
_COMPARISON_WORDS = frozenset(
    {"against", "compare", "compared", "compares", "comparing", "comparison", "versus", "vs"}
)
_COMPARISON_PHRASE_RE = re.compile(r"\b(?:up\s+or\s+down|down\s+or\s+up)\b")

_PRIOR_PERIOD_RE = re.compile(
    r"\b(?:compared\s+(?:with|to)|vs\.?|versus|against|alongside|along\s+with|next\s+to)\s+"
    r"(?:the\s+)?(?:last|prior|previous)\s+(?:fiscal\s+)?(?:day|week|month|quarter|year|period)\b",
    re.IGNORECASE,
)


def _coverage_why(gaps: list[CoverageGap]) -> dict[str, Any] | None:
    if not gaps:
        return None
    return {
        "code": "PLAN_INTENT_COVERAGE_GAP",
        "message": (
            "The draft validates as a query call, but one or more high-confidence clauses from the "
            "question are not faithfully represented. It is not ready to execute as written."
        ),
        "details": {
            "gap_count": len(gaps),
            "gaps": [gap.to_dict() for gap in gaps],
        },
        "recovery_hints": _unique_hints(gaps),
    }


def _clarify_why(gaps: list[CoverageGap], clarification: dict[str, Any]) -> dict[str, Any]:
    """``_coverage_why`` for gaps plan asks the user about, with the question to ask."""
    why = _coverage_why(gaps) or {}
    return {**why, "details": {**why.get("details", {}), "clarification": clarification}}


def _engine_query(runtime: Any, query: Call) -> dict[str, Any]:
    """The draft in the engine's own form, for the binder and catalog reads."""
    from .time_reference import time_policy_context  # noqa: WPS433

    return dict(runtime.engine_seed(query.to_call(), now=(time_policy_context() or {}).get("now")))


def _time_block(query: Call) -> Time:
    """The draft's time block; an empty one when it has none."""
    return query.time or Time()


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


def _referenced_ids(query: Call) -> list[str]:
    """Every id the draft names, its inline expressions' fields and entities too."""
    ids = query.references()
    for node in _expression_nodes(query):
        for key in ("field", "entity", "temporal_role"):
            value = node.get(key)
            if isinstance(value, str) and value and value not in ids:
                ids.append(value)
    return ids


def _expression_nodes(query: Call):
    """Every mapping inside the draft's select expressions."""
    for item in query.select:
        yield from _dict_nodes(item.expression)


def _strings(node: Any) -> Iterator[str]:
    """Every string in a nested dict/list payload."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _dict_nodes(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _dict_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _dict_nodes(child)


def _query_contains_prior_period(runtime: Any, query: Call) -> bool:
    return any(
        str(node.get("kind", "") or "").casefold() == "prior_period"
        for node in _expression_nodes(query)
    )


def _projected_subject_ids(query: Call) -> list[str]:
    """The id each select reads at its top: a reference, or the measure of an aggregate kind."""
    out: list[str] = []
    for item in query.select:
        expression = item.expression
        object_id = (
            expression
            if isinstance(expression, str)
            else expression.get("metric") or expression.get("measure")
        )
        if isinstance(object_id, str) and object_id and object_id not in out:
            out.append(object_id)
    return out


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


__all__ = [
    "CoverageGap",
]
