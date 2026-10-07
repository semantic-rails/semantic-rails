"""Thresholds, qualifying phrases and entities, and top-N requests."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ._base import _NUMBER_WORDS, _best, _dimension, _entity, _object_text, _tokens
from .time_windows import _time_window
from .visibility import visible_dimensions, visible_value_domains


def _dimension_for_value(config: Any, value: str, *, terms: Iterable[str] = ()) -> Any | None:
    value_text = str(value).lower()
    domain_dimensions: set[str] = set()
    for domain in visible_value_domains(config):
        for row in list(domain.values or []):
            values = [
                str(row.value).lower(),
                str(row.label).lower(),
                *[str(alias).lower() for alias in list(row.aliases or [])],
            ]
            if value_text in values:
                domain_dimensions.update(domain.dimensions)
    candidates = [row for row in visible_dimensions(config) if row.id in domain_dimensions]
    if candidates:
        return _best(candidates, [*terms, "product"]) or candidates[0]
    return _dimension(config, [*terms, "product"])


def _product_filter(config: Any, value: str) -> dict[str, Any] | None:
    dim = _dimension_for_value(config, value, terms=["product"])
    if dim is None:
        return None
    return {"dimension": dim.id, "op": "=", "value": value}


def _add_order(query: dict[str, Any]) -> None:
    order_by: list[dict[str, str]] = []
    if query.get("time"):
        order_by.append({"field": "time", "direction": "ASC"})
    for dim_id in list(query.get("group_by", []) or []):
        order_by.append({"field": dim_id, "direction": "ASC"})
    if order_by:
        query["order_by"] = order_by


def _threshold_value(raw: str) -> float | int:
    token = str(raw or "").lower()
    if token in _NUMBER_WORDS:
        return _NUMBER_WORDS[token]
    value = float(token)
    return int(value) if value.is_integer() else value


_PERCENTILE_PHRASE_TO_P: dict[str, float] = {
    "top decile": 0.9,
    "top tenth": 0.9,
    "top quintile": 0.8,
    "top quartile": 0.75,
    "top quarter": 0.75,
    "top third": 0.6667,
    "top half": 0.5,
}


def _threshold_from_text(text: str) -> tuple[str, float | int | dict[str, Any]] | None:
    lowered = str(text or "").lower()
    for phrase, p in _PERCENTILE_PHRASE_TO_P.items():
        if phrase in lowered:
            return ">=", {"kind": "percentile", "p": p}
    top_percent_match = re.search(r"top\s+(\d+(?:\.\d+)?)\s*(?:%|percent|pct)", lowered)
    if top_percent_match:
        try:
            n = float(top_percent_match.group(1))
        except ValueError:
            n = 0.0
        if 0.0 < n < 100.0:
            return ">=", {"kind": "percentile", "p": round((100.0 - n) / 100.0, 4)}
    percentile_match = re.search(
        r"(?:above|in|in the top)\s+(?:the\s+)?(\d+(?:\.\d+)?)\s*(?:st|nd|rd|th)?\s*percentile",
        lowered,
    )
    if percentile_match:
        try:
            n = float(percentile_match.group(1))
        except ValueError:
            n = 0.0
        if 0.0 < n < 100.0:
            return ">", {"kind": "percentile", "p": round(n / 100.0, 4)}
    percent_patterns = [
        (
            r"(?:more than|greater than|over|above|exceeded|exceeds|exceeding)\s+(\d+(?:\.\d+)?)\s*(?:%|percent|pct)",
            ">",
        ),
        (
            r"(?:at least|greater than or equal to|minimum of)\s+(\d+(?:\.\d+)?)\s*(?:%|percent|pct)",
            ">=",
        ),
        (r"(?:less than|under|below)\s+(\d+(?:\.\d+)?)\s*(?:%|percent|pct)", "<"),
        (r"(\d+(?:\.\d+)?)\s*(?:%|percent|pct)", ">"),
    ]
    for pattern, op in percent_patterns:
        match = re.search(pattern, lowered)
        if match:
            return op, float(match.group(1)) / 100.0
    patterns = [
        (
            r"(?:at least|greater than or equal to|no fewer than|minimum of)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)",
            ">=",
        ),
        (r"(\d+)\s*\+", ">="),
        (
            r"(?:more than|greater than|over|above|exceeded|exceeds|exceeding)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)",
            ">",
        ),
        (
            r"(?:less than|fewer than|under|below)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)",
            "<",
        ),
        (
            r"(?:at most|no more than)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)",
            "<=",
        ),
    ]
    for pattern, op in patterns:
        match = re.search(pattern, lowered)
        if match:
            return op, _threshold_value(match.group(1))
    if "activity" in lowered or "active" in lowered:
        return ">", 0
    return None


def _target_measure_terms(text: str, terms: set[str]) -> list[str]:
    if "aov" in terms or {"average", "order", "value"} <= terms:
        return ["average", "order", "value"]
    if {"session", "order", "conversion"} <= terms:
        return ["session", "order", "conversion", "rate"]
    if {"signup", "adoption"} <= terms or {"signup", "conversion"} <= terms:
        return ["signup", "send", "adoption", "conversion"]
    if {"month", "revenue", "growth"} <= terms:
        return ["month", "over", "month", "revenue", "growth"]
    if {"new", "customer", "order"} <= terms:
        return ["new", "customer", "order"]
    if {"repeat", "customer", "order"} <= terms:
        return ["repeat", "customer", "order"]
    if {"high", "value", "customer", "order"} <= terms:
        return ["high", "value", "customer", "order"]
    if "menu" in terms and ("active" in terms or "snapshot" in terms):
        return ["active", "menu"]
    if "inventory" in terms or "snapshot" in terms:
        return ["inventory"]
    if "arr" in terms:
        return ["arr"]
    if "revenue" in terms or "sales" in terms:
        qualifiers = [
            token for token in ("delivered", "drink", "food", "cost", "tax") if token in terms
        ]
        return [*qualifiers, "revenue"]
    if "order" in terms or "volume" in terms:
        return ["order"]
    if "logo" in terms:
        return ["logo"]
    return []


_QUALIFICATION_TRIGGERS = (
    "with at least",
    "with more than",
    "with at most",
    "with fewer than",
    "with less than",
    "with over",
    "with under",
    "with above",
    "with below",
    "having at least",
    "having more than",
    "having at most",
    "having fewer than",
    "having less than",
    "having over",
    "having under",
    "who made more than",
    "who made at least",
    "who made over",
    "that made more than",
    "that made at least",
    "who placed more than",
    "who placed at least",
    "who placed over",
    "who placed under",
    "that placed more than",
    "that placed at least",
    "who have more than",
    "who have at least",
    "that have more than",
    "that have at least",
    "that have done more than",
    "that have done at least",
    "that have an order rate of over",
    "for stores that have",
    "for customers that have",
    "for customers who",
    "top decile of",
    "top tenth of",
    "top quintile of",
    "top quartile of",
    "top quarter of",
    "top third of",
    "top half of",
)


_PERCENTILE_FRACTION_TRIGGER_RE = re.compile(
    r"top\s+\d+(?:\.\d+)?\s*(?:%|percent|pct|st|nd|rd|th)?\s*(?:percentile\s+)?of\s+",
    re.IGNORECASE,
)


def _qualification_phrase(text: str) -> str:
    """Return the qualification phrase from the intent, or "".

    Kept in the planner helper layer because several intent patterns
    share the same qualification parsing policy.
    """
    lowered = str(text or "").lower()
    for trigger in _QUALIFICATION_TRIGGERS:
        idx = lowered.find(trigger)
        if idx >= 0:
            return lowered[idx + len(trigger) :].strip()
    fraction_match = _PERCENTILE_FRACTION_TRIGGER_RE.search(lowered)
    if fraction_match:
        return lowered[fraction_match.end() :].strip()
    return ""


_QUALIFICATION_STOP_TOKENS = frozenset(
    {
        "and",
        "or",
        "more",
        "than",
        "least",
        "at",
        "fewer",
        "less",
        "over",
        "under",
        "above",
        "below",
        "exactly",
        "the",
        "a",
        "an",
        "of",
        "by",
        "for",
        "from",
        "in",
        "on",
        "with",
        "without",
        "grouped",
        "group",
        "during",
        "between",
        "month",
        "day",
        "week",
        "quarter",
        "year",
        "purchase",
        "purchases",
        "purchased",
        "made",
        "make",
        "done",
        "do",
        "did",
        "have",
        "had",
        "has",
        "having",
        "that",
        "who",
    }
)


def _qualification_phrase_token_groups(text: str, target_terms: list[str]) -> list[list[str]]:
    """Return one token group per AND/OR-joined sub-qualification."""
    phrase = _qualification_phrase(text)
    if not phrase:
        return []
    target_set = {term for term in target_terms if term}
    parts = re.split(r"\s+(?:and|or)\s+", phrase)
    groups: list[list[str]] = []
    for part in parts:
        tokens_raw = _tokens(part)
        seen_raw: set[str] = set()
        all_tokens: list[str] = []
        for token in tokens_raw:
            if token.isdigit() or token in _NUMBER_WORDS:
                continue
            if token in _QUALIFICATION_STOP_TOKENS:
                continue
            if token in seen_raw:
                continue
            seen_raw.add(token)
            all_tokens.append(token)
        if not all_tokens:
            continue
        non_target = [token for token in all_tokens if token not in target_set]
        if non_target:
            groups.append(non_target)
        elif target_set and set(all_tokens).issubset(target_set):
            groups.append(list(target_terms))
    return groups


_EXPLICIT_QUALIFYING_ENTITY_PATTERNS = (
    (r"\bfor\s+customer", "customer"),
    (r"\bfor\s+store", "store"),
    (r"\bfor\s+account", "account"),
    (r"\bfrom\s+customer", "customer"),
    (r"\bfrom\s+store", "store"),
    (r"\bfrom\s+account", "account"),
    (r"\bcustomer[s]?\s+(?:who|that|with|having)\s+", "customer"),
    (r"\bstore[s]?\s+(?:who|that|with|having)\s+", "store"),
    (r"\baccount[s]?\s+(?:who|that|with|having)\s+", "account"),
)


def _explicit_qualifying_entity_keyword(text: str) -> str:
    lowered = str(text or "").lower()
    for pattern, keyword in _EXPLICIT_QUALIFYING_ENTITY_PATTERNS:
        if re.search(pattern, lowered):
            return keyword
    return ""


def _qualifying_entity(config: Any, terms: set[str], *, text: str = "") -> Any | None:
    explicit = _explicit_qualifying_entity_keyword(text)
    if explicit == "customer":
        return _entity(config, ["customer"])
    if explicit == "store":
        return _entity(config, ["store"])
    if explicit == "account":
        return _entity(
            config,
            ["account"],
            exclude=[row.id for row in config.entities if "parent" in _object_text(row)],
        )
    if "store" in terms and "customer" in terms:
        return _entity(config, ["store"])
    if "account" in terms and "customer" in terms:
        return _entity(
            config,
            ["account"],
            exclude=[row.id for row in config.entities if "parent" in _object_text(row)],
        )
    if "customer" in terms:
        return _entity(config, ["customer"])
    if "store" in terms:
        return _entity(config, ["store"])
    if bool({"entity", "entities", "child"} & terms) and "order" in terms and "rate" in terms:
        return _entity(config, ["store"])
    if "account" in terms:
        return _entity(
            config,
            ["account"],
            exclude=[row.id for row in config.entities if "parent" in _object_text(row)],
        )
    return None


_TOP_N_PATTERN = re.compile(r"\btop(?:\s+(\d+))?\b", re.IGNORECASE)
# Recognize "which N <noun> ... highest/most/best/largest" phrasing,
# e.g. "which 3 stores have the highest revenue". The blind-agent
# usability test flagged this — the user clearly meant top-3 but the
# pattern only matched literal "top N".
_RANK_PATTERN = re.compile(
    r"\b(?:which|what|name|show|give|list)\b[^.?!]*?\b(\d+)\b[^.?!]*?"
    r"\b(?:highest|most|best|largest|biggest|greatest|smallest|fewest|lowest|worst|by)\b",
    re.IGNORECASE,
)
_DEFAULT_TOP_LIMIT = 5


def _top_n_intent(text: str) -> tuple[bool, int]:
    """Return (is_top_intent, n). n is the parsed top-N (defaults to 5).

    Matches both literal "top N" and the looser "which N <noun> …
    highest/most/best/largest/by" phrasing surfaced by blind-agent
    feedback ("which 3 stores have the highest revenue"). A number in a
    time phrase ("in the last 3 months by store", "in 2017") is not a rank.
    """

    lowered = str(text or "").lower()  # the case the time spans index
    match = _TOP_N_PATTERN.search(lowered)
    if match:
        raw = match.group(1)
        if not raw:
            return True, _DEFAULT_TOP_LIMIT
        try:
            return True, int(raw)
        except ValueError:
            return True, _DEFAULT_TOP_LIMIT
    for start, end in _time_window(lowered).spans:
        lowered = lowered[:start] + " " * (end - start) + lowered[end:]
    rank_match = _RANK_PATTERN.search(lowered)
    if rank_match:
        try:
            return True, int(rank_match.group(1))
        except ValueError:
            return True, _DEFAULT_TOP_LIMIT
    return False, _DEFAULT_TOP_LIMIT
