"""Shared helpers for intent patterns.

Patterns under ``semantic_rails/planner/patterns/`` import what they
need from this module so the orchestrator stays thin.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from ..expressions import MeasureRefExpr, expr_to_dict
from .visibility import visible_dimensions


@dataclass(frozen=True)
class RuntimeCompositionDraft:
    query: dict[str, Any]
    resolved: list[dict[str, Any]]
    rationale: list[str]
    interpreted_intent: dict[str, Any]
    # When non-empty, the draft is intentionally non-executable and plan
    # should surface it as a blocked entry with this {code, message,
    # details, recovery_hints} envelope. Used by patterns that detect
    # intent but cannot synthesize a legal IR (e.g.
    # ``qualified_metric_rollup`` when no predicate metric resolves from
    # the qualification phrase).
    blocked_reason: dict[str, Any] = field(default_factory=dict)
    # Confidence the pattern is the right match for this intent, on
    # [0, 1]. Used by the orchestrator's score-based selection to pick
    # between competing patterns. Defaults to 1.0 (the pattern is
    # certain). Catch-all patterns return a lower score so they only
    # win when no more specific pattern claimed the intent.
    score: float = 1.0


_TERM_SYNONYMS = {
    "geography": "geo",
    "geographic": "geo",
    "region": "geo",
    "regions": "geo",
    "customer": "customer",
    "customers": "customer",
    "store": "store",
    "stores": "store",
    "order": "order",
    "orders": "order",
    "purchase": "order",
    "purchases": "order",
    "ordered": "order",
    "session": "session",
    "sessions": "session",
    "signup": "signup",
    "signups": "signup",
    "adoption": "adoption",
    "activation": "adoption",
    "funnel": "funnel",
    "funnels": "funnel",
    "menu": "menu",
    "inventory": "inventory",
    "snapshot": "snapshot",
    "snapshots": "snapshot",
    "sent": "received",
    "send": "received",
    "sends": "received",
    "messages": "message",
    "logos": "logo",
    "accounts": "account",
    "beneficiaries": "beneficiary",
    "claims": "claim",
    "codes": "code",
    "diagnoses": "diagnosis",
    "procedures": "procedure",
    "percent": "pct",
    "percentage": "pct",
    "monthly": "month",
    "daily": "day",
    "weekly": "week",
    "quarterly": "quarter",
    "yearly": "year",
}

_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


@lru_cache(maxsize=8192)
def _cached_tokens(text: str) -> tuple[str, ...]:
    raw = "".join(ch.lower() if ch.isalnum() else " " for ch in str(text or ""))
    out: list[str] = []
    for token in raw.split():
        out.append(_TERM_SYNONYMS.get(token, token))
    return tuple(out)


def _tokens(text: str) -> tuple[str, ...]:
    return _cached_tokens(str(text or ""))


def _singular(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _object_text(row: Any) -> str:
    parts = [
        getattr(row, "id", ""),
        getattr(row, "name", ""),
        getattr(row, "label", ""),
        getattr(row, "description", ""),
        " ".join(getattr(row, "aliases", []) or []),
        " ".join(getattr(row, "topics", []) or []),
    ]
    return " ".join(parts).lower()


def _score(row: Any, terms: Iterable[str]) -> int:
    object_text = _object_text(row)
    text_tokens = set(_tokens(object_text))
    primary_tokens = set(
        _tokens(
            " ".join(
                [
                    getattr(row, "id", ""),
                    getattr(row, "name", ""),
                    getattr(row, "label", ""),
                ]
            )
        )
    )
    score = 0
    for term in terms:
        if term in text_tokens or term in object_text:
            score += 1
        if term in primary_tokens:
            score += 2
    return score


def _best(
    rows: Iterable[Any],
    terms: Iterable[str],
    *,
    require_all: bool = False,
    exclude: Iterable[str] = (),
) -> Any | None:
    term_list = [term for term in terms if term]
    excluded = set(exclude)
    ranked = []
    for row in rows:
        if getattr(row, "id", "") in excluded:
            continue
        row_score = _score(row, term_list)
        if require_all and row_score < len(set(term_list)):
            continue
        if row_score > 0:
            ranked.append((row_score, getattr(row, "label", ""), getattr(row, "id", ""), row))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    return ranked[0][3] if ranked else None


_EXTRA_QUALIFIER_TERMS = (
    "delivered",
    "cumulative",
    "mtd",
    "qtd",
    "ytd",
    "drink",
    "food",
    "new",
    "cost",
    "tax",
    "primary",
    "payer",
    "allowed",
    "liability",
    "annual",
    "lifetime",
    "average",
    "mean",
    "median",
)


def _specificity_penalty(row: Any, terms: Iterable[str]) -> int:
    term_set = set(terms)
    text = _object_text(row)
    return sum(2 for item in _EXTRA_QUALIFIER_TERMS if item in text and item not in term_set)


def _measure_metric_id(config: Any, measure_id: str) -> str:
    for recipe in config.metric_recipes:
        expr = getattr(recipe, "expression", None)
        if isinstance(expr, MeasureRefExpr) and expr.measure == measure_id:
            return recipe.id
        payload = expr_to_dict(expr) if expr is not None else {}
        if payload.get("measure") == measure_id:
            return recipe.id
    return ""


def _resolved(row: Any, *, object_type: str = "") -> dict[str, Any]:
    return {
        "id": getattr(row, "id", ""),
        "object_type": object_type or getattr(row, "id", "").split(".", 1)[0],
        "label": getattr(row, "label", getattr(row, "id", "")),
    }


def _entity(config: Any, terms: Iterable[str], *, exclude: Iterable[str] = ()) -> Any | None:
    return _best(config.entities, terms, exclude=exclude)


def _measure(config: Any, terms: Iterable[str]) -> Any | None:
    return _best(config.measures, terms)


def _metric(config: Any, terms: Iterable[str]) -> Any | None:
    return _best(config.metric_recipes, terms)


def _object_by_id(rows: Iterable[Any], object_id: str) -> Any | None:
    return next((row for row in rows if getattr(row, "id", "") == object_id), None)


def _last_token(value: str) -> str:
    return str(value or "").split(".")[-1]


def _slug(value: str, *, fallback: str = "value") -> str:
    raw = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or ""))
    parts = [part for part in raw.split("_") if part]
    return "_".join(parts) or fallback


def _semantic_token(value: str, *, fallback: str = "value") -> str:
    raw_value = str(value or "")
    token = _last_token(value)
    if raw_value.startswith("entity.") and "_" in token:
        token = token.split("_", 1)[1]
    for prefix in ("jaffle_", "entity_", "metric_recipe_", "measure_"):
        if token.startswith(prefix):
            token = token[len(prefix) :]
    return _slug(token, fallback=fallback)


def _tied_top(rows: Iterable[Any], terms: set[str], words: set[str]) -> tuple[list[Any], Any]:
    """The rows sharing the ranking's top score, and the one the question names, if any.

    Rows rank by ``_score`` less the specificity penalty. The question's
    ``words`` name a tied row over another when its names (label, key and
    aliases) match every word the other's do and one more, or the same words
    and one of its names whole: "revenue" names Revenue over Item Revenue
    Cents, "item revenue" the reverse. A name missing only its "count" is
    nearly whole: "number of customers" names Customer count over Ordering
    customers. "revenue before tax" names neither Revenue nor Tax paid, so
    the first by label stays a guess.
    """

    matched = [(_score(row, terms), row) for row in rows]
    scored = [
        (score - _specificity_penalty(row, terms), row) for score, row in matched if score > 0
    ]
    top = max((score for score, _row in scored), default=0)
    tied = sorted(
        (row for score, row in scored if score == top > 0),
        key=lambda row: (row.label, row.id),
    )
    fit = {}
    for row in tied:
        names = [row.label, _last_token(row.id), *(getattr(row, "aliases", None) or [])]
        sets = [set(_tokens(name)) for name in names if _tokens(name)]
        whole = 2 if any(name <= words for name in sets) else 0
        if not whole and any(len(name) > 1 and name - {"count"} <= words for name in sets):
            whole = 1
        fit[row.id] = (set().union(*sets) & words, whole)

    def beats(row: Any, other: Any) -> bool:
        (said, whole), (other_said, other_whole) = fit[row.id], fit[other.id]
        return other_said < said or (other_said == said and whole > other_whole)

    named = [row for row in tied if all(beats(row, other) for other in tied if other is not row)]
    return tied, next(iter(named), None)


def _top(rows: Iterable[Any], terms: set[str], words: Iterable[str]) -> Any | None:
    tied, named = _tied_top(rows, terms, set(words) or terms)
    return named or next(iter(tied), None)


def _preferred_measure(config: Any, terms: Iterable[str], words: Iterable[str] = ()) -> Any | None:
    """The canonical measure for ``terms``, else the top-ranked one the question
    names, else the first of those. ``words`` are the question's own words for
    the measure, when ``terms`` keep only some of them."""

    term_set = set(terms)
    return _canonical_measure(config, term_set) or _top(config.measures, term_set, words)


def _canonical_measure(config: Any, term_set: set[str]) -> Any | None:
    preferred_ids: list[str] = []
    if {"new", "customer", "order"} <= term_set:
        preferred_ids.append("measure.jaffle.new_customer_order_count")
    qualified_revenue_terms = {"delivered", "drink", "food", "cost", "tax"}
    if "revenue" in term_set and not (term_set & qualified_revenue_terms):
        preferred_ids.append("measure.jaffle.revenue_usd")
    if "order" in term_set and len(term_set - {"order"}) == 0:
        preferred_ids.append("measure.jaffle.order_count")
    for measure_id in preferred_ids:
        match = next((row for row in config.measures if row.id == measure_id), None)
        if match is not None:
            return match
    return None


def _preferred_metric(config: Any, terms: Iterable[str], words: Iterable[str] = ()) -> Any | None:
    """``_preferred_measure`` for metrics."""

    term_set = set(terms)
    return _canonical_metric(config, term_set) or _top(config.metric_recipes, term_set, words)


def _canonical_metric(config: Any, term_set: set[str]) -> Any | None:
    preferred_ids: list[str] = []
    if "aov" in term_set or {"average", "order", "value"} <= term_set:
        preferred_ids.append("metric.sales.aov_usd")
    if {"session", "order", "conversion"} <= term_set:
        preferred_ids.append("metric.sales.session_to_order_conversion_rate_7d")
    if {"signup", "adoption"} <= term_set or {"signup", "conversion"} <= term_set:
        preferred_ids.append("metric.adoption.signup_to_send_conversion_rate_28d")
    if {"month", "revenue", "growth"} <= term_set:
        preferred_ids.append("metric.sales.month_over_month_revenue_growth")
    if {"repeat", "customer", "order"} <= term_set:
        preferred_ids.append("metric.sales.repeat_customer_orders")
    if {"high", "value", "customer", "order"} <= term_set:
        preferred_ids.append("metric.sales.high_value_customer_orders")
    if "order" in term_set and len(term_set - {"order"}) == 0:
        preferred_ids.append("metric.sales.orders")
    if "revenue" in term_set:
        preferred_ids.append("metric.sales.revenue_usd")
    for metric_id in preferred_ids:
        match = next((row for row in config.metric_recipes if row.id == metric_id), None)
        if match is not None:
            return match
    return None


def _declared_name_forms(row: Any) -> list[str]:
    """The declared forms of one name, shared by selection and readiness."""

    label = str(getattr(row, "label", "") or "")
    return [
        label,
        re.sub(r"\s*\(.*?\)", "", label),
        str(row.id),
        _last_token(row.id),
        *(getattr(row, "aliases", None) or []),
    ]


def _name_matches(row: Any, text: str, *, short_label: bool = True) -> list[tuple[int, int, int]]:
    """Whole contiguous declared names, as (word count, start, end) spans."""

    words = list(re.finditer(r"[^\W_]+", text.lower()))
    said = [_singular(word.group()) for word in words]
    matches = set()
    forms = _declared_name_forms(row)
    for name in forms if short_label else [forms[0], *forms[2:]]:
        parts = [_singular(word) for word in re.findall(r"[^\W_]+", name.lower())]
        for start in range(len(said) - len(parts) + 1):
            if parts and said[start : start + len(parts)] == parts:
                matches.add((len(parts), words[start].start(), words[start + len(parts) - 1].end()))
    return sorted(matches)


def _name_fit(text: str, spans: Iterable[tuple[int, int, int]]) -> set[str]:
    """The words inside a subject's matched name spans; no other word of the question."""

    return {
        _singular(word)
        for _, start, end in spans
        for word in re.findall(r"[^\W_]+", text[start:end].lower())
    }


def _named_metric(config: Any, text: str) -> tuple[Any, str] | None:
    """A whole metric name containing every named measure, replaced by its id.

    Ties never select a metric. Single-word synonyms also name a metric; label and
    id forms retain the multi-word requirement used for ordinary measure-first lookup.
    A label without its parenthetical never selects: it is a readiness form only.
    """

    spans = {row.id: _name_matches(row, text, short_label=False) for row in config.metric_recipes}
    matches = [
        (size, start, end, row)
        for row in config.metric_recipes
        for size, start, end in spans[row.id]
        if size > 1
        or any(
            _singular(alias.lower()) == _singular(text[start:end].lower())
            for alias in (row.aliases or [])
        )
    ]
    size = max((item[0] for item in matches), default=0)
    fits = {row.id: _name_fit(text, spans[row.id]) for _, _, _, row in matches}
    longest = [
        item
        for item in matches
        if item[0] == size and not any(fits[item[3].id] < words for words in fits.values())
    ]
    if not longest or len({item[3].id for item in longest}) != 1:
        return None
    _, first, last, metric = longest[0]
    if any(
        not (first <= begin and end <= last)
        for row in config.measures
        for _size, begin, end in _name_matches(row, text)
    ):
        return None
    return metric, f"{text[:first]}{metric.id}{text[last:]}"


def _said_name(row: Any, text: str) -> frozenset[str]:
    """Words of the longest whole name said, allowing plurals.

    Labels and ids retain their any-order rule. A multi-word synonym must be
    contiguous, so separate fragments cannot stand in for its declared phrase.
    """

    said = {_singular(word) for word in _tokens(text)}
    aliases = getattr(row, "aliases", None) or []
    contiguous = {text[start:end].lower() for _, start, end in _name_matches(row, text)}
    return max(
        (
            words
            for name in _declared_name_forms(row)
            if (words := frozenset(map(_singular, _tokens(name)))) <= said
            and (
                name not in aliases
                or len(words) == 1
                or any(frozenset(map(_singular, _tokens(span))) == words for span in contiguous)
            )
        ),
        key=len,
        default=frozenset(),
    )


_NAME_CONNECTORS = frozenset(
    {"a", "across", "along", "and", "at", "by", "during", "for", "from", "in", "of", "on", "over"}
    | {"per", "the", "to", "with"}
)


def _aggregation_from_text(text: str, terms: set[str], measure: Any) -> str:
    allowed = set(getattr(measure, "allowed_aggregations", []) or [])
    if ("sum" in terms or "total" in terms) and "sum" in allowed:
        return "sum"
    return str(getattr(measure, "default_aggregation", "") or "sum")


def _dimension(config: Any, terms: Iterable[str], *, prefer_parent: bool = False) -> Any | None:
    candidates = []
    for row in visible_dimensions(config):
        row_score = _score(row, terms)
        if row_score <= 0:
            continue
        if prefer_parent and "parent" in _object_text(row):
            row_score += 2
        candidates.append((row_score, getattr(row, "label", ""), getattr(row, "id", ""), row))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    return candidates[0][3] if candidates else None


def _runtime_composition_terms(text: str) -> set[str]:
    return set(_tokens(text))
