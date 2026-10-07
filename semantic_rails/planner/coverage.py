from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

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
