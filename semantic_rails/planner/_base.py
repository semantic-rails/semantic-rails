"""Shared helpers for intent patterns.

Patterns under ``semantic_rails/planner/patterns/`` import what they
need from this module so the orchestrator stays thin.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from typing import Any

from ..expressions import MeasureRefExpr, expr_to_dict


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


def _preferred_measure(config: Any, terms: Iterable[str]) -> Any | None:
    term_set = set(terms)
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
    ranked = []
    for row in config.measures:
        score = _score(row, term_set)
        if score <= 0:
            continue
        score -= _specificity_penalty(row, term_set)
        ranked.append((score, getattr(row, "label", ""), getattr(row, "id", ""), row))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    return ranked[0][3] if ranked and ranked[0][0] > 0 else None


def _preferred_metric(config: Any, terms: Iterable[str]) -> Any | None:
    term_set = set(terms)
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
    ranked = []
    for row in config.metric_recipes:
        score = _score(row, term_set)
        if score <= 0:
            continue
        score -= _specificity_penalty(row, term_set)
        ranked.append((score, getattr(row, "label", ""), getattr(row, "id", ""), row))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    return ranked[0][3] if ranked and ranked[0][0] > 0 else None


def _aggregation_from_text(text: str, terms: set[str], measure: Any) -> str:
    allowed = set(getattr(measure, "allowed_aggregations", []) or [])
    if ("sum" in terms or "total" in terms) and "sum" in allowed:
        return "sum"
    return str(getattr(measure, "default_aggregation", "") or "sum")


def _dimension(config: Any, terms: Iterable[str], *, prefer_parent: bool = False) -> Any | None:
    candidates = []
    for row in config.dimensions:
        row_score = _score(row, terms)
        if row_score <= 0:
            continue
        if prefer_parent and "parent" in _object_text(row):
            row_score += 2
        candidates.append((row_score, getattr(row, "label", ""), getattr(row, "id", ""), row))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    return candidates[0][3] if candidates else None


def _dimension_for_value(config: Any, value: str, *, terms: Iterable[str] = ()) -> Any | None:
    value_text = str(value).lower()
    domain_dimensions: set[str] = set()
    for domain in config.value_domains:
        for row in list(domain.values or []):
            values = [
                str(row.value).lower(),
                str(row.label).lower(),
                *[str(alias).lower() for alias in list(row.aliases or [])],
            ]
            if value_text in values:
                domain_dimensions.update(domain.dimensions)
    candidates = [row for row in config.dimensions if row.id in domain_dimensions]
    if candidates:
        return _best(candidates, [*terms, "product"]) or candidates[0]
    return _dimension(config, [*terms, "product"])


def _explicit_grain(text: str) -> str:
    """Return the grain the intent explicitly cues ("" when absent)."""
    lowered = str(text or "").lower()
    terms = _runtime_composition_terms(text)
    if re.search(r"\bq[1-4]\s*['-]?\s*20\d{2}\b", lowered):
        return "quarter"
    for candidate in _TIME_UNITS:
        if (
            f"by {candidate}" in lowered
            or f"per {candidate}" in lowered
            or f"each {candidate}" in lowered
            or f"{candidate}ly" in lowered
            or candidate in terms
        ):
            return candidate
    return ""


def _time_spec(role: str, text: str) -> dict[str, Any]:
    lowered = str(text or "").lower()
    grain = _explicit_grain(text)
    bounds = _time_bounds_from_text(text)
    if not grain:
        # No explicit grain cue — when the intent resolved to a window
        # ("last 7 days", "yesterday"), bucket at the window's own unit
        # instead of the generic month default.
        grain = _implied_window_grain(lowered) or "month"
    return {"temporal_role": role, "grain": grain, **bounds}


def _first_day_of_next_month(value: date) -> date:
    return (value.replace(day=28) + timedelta(days=4)).replace(day=1)


_TIME_UNITS = ("day", "week", "month", "quarter", "year")
_TIME_UNIT_ALT = "|".join(_TIME_UNITS)
_NUMBER_WORD_ALT = "|".join(_NUMBER_WORDS)

# Comparison phrasings ("vs last month", "compared to last year") belong
# to the period-shift patterns (``_PERIOD_SHIFT_TRIGGERS``), not the
# window resolver — bounding the query there would truncate the rows the
# prior-period lookback needs. Each lookbehind alternative is fixed
# width as Python's ``re`` requires.
_COMPARISON_GUARD = (
    r"(?<!vs )(?<!vs\. )(?<!versus )(?<!compared to )(?<!relative to )(?<!than )(?<!since )"
)

# Relative trailing windows the planner resolves into the Query IR's
# relative range form (``time.range.last``): "last month",
# "last 7 days", "past three quarters", "trailing 12 months", ...
_RELATIVE_WINDOW_RE = re.compile(
    rf"{_COMPARISON_GUARD}\b(?:last|past|previous|prior|trailing)\s+"
    rf"(?:(\d+|{_NUMBER_WORD_ALT})\s+)?({_TIME_UNIT_ALT})s?\b"
)
_YESTERDAY_RE = re.compile(rf"{_COMPARISON_GUARD}\byesterday\b")
_TODAY_RE = re.compile(rf"{_COMPARISON_GUARD}\btoday\b")
_THIS_PERIOD_RE = re.compile(rf"{_COMPARISON_GUARD}\b(?:current|this)\s+({_TIME_UNIT_ALT})\b")
_QUARTER_YEAR_RE = re.compile(r"\bq([1-4])\s*['-]?\s*(20\d{2})\b")

# Explicit historical month ranges ("from January 2017 through June
# 2017", "Jan-Jun 2017", "between March and May 2024"). External-agent
# feedback: these bounds were silently dropped from the Query IR, so a
# fixed historical ask quietly became an unbounded scan. Month names
# require an adjacent year (directly or via the shared-year range form)
# so bare "may"/"march" verbs never resolve as dates.
_MONTH_NUMBERS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
_MONTH_ALT = "|".join(
    sorted(_MONTH_NUMBERS, key=len, reverse=True)
)  # longest-first so "june" wins over "jun"
_RANGE_CONNECTOR = r"(?:through|thru|until|till|to|and|[-–—])"
_MONTH_YEAR_RANGE_RE = re.compile(
    rf"{_COMPARISON_GUARD}\b({_MONTH_ALT})\.?\s+(20\d{{2}})\s*{_RANGE_CONNECTOR}\s*"
    rf"({_MONTH_ALT})\.?\s+(20\d{{2}})\b"
)
_MONTH_RANGE_SHARED_YEAR_RE = re.compile(
    rf"{_COMPARISON_GUARD}\b({_MONTH_ALT})\.?\s*{_RANGE_CONNECTOR}\s*"
    rf"({_MONTH_ALT})\.?\s+(20\d{{2}})\b"
)
_SINGLE_MONTH_YEAR_RE = re.compile(rf"{_COMPARISON_GUARD}\b({_MONTH_ALT})\.?\s+(20\d{{2}})\b")

# Temporal cues the planner recognizes as a time scope but cannot turn
# into bounds ("last few weeks", "past holiday season", "since 2023").
# Superset of the resolvable shapes above; ``_unresolved_time_phrases``
# subtracts the spans the resolver actually handled.
_TEMPORAL_CUE_RE = re.compile(
    rf"{_COMPARISON_GUARD}\b(?:last|past|previous|prior|trailing)\s+"
    rf"(?:(?:few|couple(?:\s+of)?|several|\d+|{_NUMBER_WORD_ALT})\s+)?"
    rf"(?:{_TIME_UNIT_ALT}|fortnight|holiday|season|summer|winter|spring|fall|autumn|half)s?\b"
)
_SINCE_CUE_RE = re.compile(
    r"\bsince\s+(?:the\s+)?(?:\d{4}|q[1-4]\b|last|this|yesterday"
    r"|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?"
    r"|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
)


def _relative_window_value(raw: str) -> int:
    token = str(raw or "").strip().lower()
    if not token:
        return 1
    if token in _NUMBER_WORDS:
        return _NUMBER_WORDS[token]
    try:
        return max(1, int(token))
    except ValueError:
        return 1


def _current_period_bounds(unit: str) -> dict[str, str]:
    today = date.today()
    if unit == "day":
        return {"start": today.isoformat(), "end": (today + timedelta(days=1)).isoformat()}
    if unit == "week":
        start = today - timedelta(days=today.weekday())
        return {"start": start.isoformat(), "end": (start + timedelta(days=7)).isoformat()}
    if unit == "month":
        start = today.replace(day=1)
        return {"start": start.isoformat(), "end": _first_day_of_next_month(today).isoformat()}
    if unit == "quarter":
        start_month = ((today.month - 1) // 3) * 3 + 1
        start = date(today.year, start_month, 1)
        end_year = today.year + (1 if start_month == 10 else 0)
        end_month = 1 if start_month == 10 else start_month + 3
        return {"start": start.isoformat(), "end": date(end_year, end_month, 1).isoformat()}
    # year
    return {
        "start": date(today.year, 1, 1).isoformat(),
        "end": date(today.year + 1, 1, 1).isoformat(),
    }


def _month_start(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}-01"


def _month_end_exclusive(year: int, month: int) -> str:
    if month == 12:
        return _month_start(year + 1, 1)
    return _month_start(year, month + 1)


def _month_range_bounds(lowered: str) -> dict[str, str]:
    """Bounds for explicit month/year phrases, or ``{}``.

    End bounds are exclusive, matching the Query IR contract: "through
    June 2017" yields ``end: 2017-07-01``.
    """

    range_match = _MONTH_YEAR_RANGE_RE.search(lowered)
    if range_match:
        start_month = _MONTH_NUMBERS[range_match.group(1)]
        start_year = int(range_match.group(2))
        end_month = _MONTH_NUMBERS[range_match.group(3)]
        end_year = int(range_match.group(4))
        return {
            "start": _month_start(start_year, start_month),
            "end": _month_end_exclusive(end_year, end_month),
        }
    shared_match = _MONTH_RANGE_SHARED_YEAR_RE.search(lowered)
    if shared_match:
        year = int(shared_match.group(3))
        return {
            "start": _month_start(year, _MONTH_NUMBERS[shared_match.group(1)]),
            "end": _month_end_exclusive(year, _MONTH_NUMBERS[shared_match.group(2)]),
        }
    single_match = _SINGLE_MONTH_YEAR_RE.search(lowered)
    if single_match:
        year = int(single_match.group(2))
        month = _MONTH_NUMBERS[single_match.group(1)]
        return {
            "start": _month_start(year, month),
            "end": _month_end_exclusive(year, month),
        }
    return {}


def _time_bounds_from_text(text: str) -> dict[str, Any]:
    lowered = str(text or "").lower()
    month_bounds = _month_range_bounds(lowered)
    if month_bounds:
        return month_bounds
    q_match = _QUARTER_YEAR_RE.search(lowered)
    if q_match:
        quarter = int(q_match.group(1))
        year = int(q_match.group(2))
        start_month = ((quarter - 1) * 3) + 1
        end_year = year + (1 if quarter == 4 else 0)
        end_month = 1 if quarter == 4 else start_month + 3
        return {
            "start": f"{year:04d}-{start_month:02d}-01",
            "end": f"{end_year:04d}-{end_month:02d}-01",
        }
    relative_match = _RELATIVE_WINDOW_RE.search(lowered)
    if relative_match:
        return {
            "range": {
                "last": {
                    "unit": relative_match.group(2),
                    "value": _relative_window_value(relative_match.group(1)),
                }
            }
        }
    if _YESTERDAY_RE.search(lowered):
        # ``range.last`` floors ``end`` to the start of the current
        # period, so {day, 1} is exactly yesterday (end-exclusive).
        return {"range": {"last": {"unit": "day", "value": 1}}}
    this_match = _THIS_PERIOD_RE.search(lowered)
    if this_match:
        return _current_period_bounds(this_match.group(1))
    if _TODAY_RE.search(lowered):
        return _current_period_bounds("day")
    return {}


def _implied_window_grain(lowered: str) -> str:
    """Grain implied by a resolved relative window, or ``""``.

    "last 7 days" implies a daily series; "yesterday"/"today" imply a
    single day bucket. Calendar-quarter and this-period phrases already
    carry their grain via the explicit cue scan in ``_time_spec``.
    """

    relative_match = _RELATIVE_WINDOW_RE.search(lowered)
    if relative_match:
        return relative_match.group(2)
    if _YESTERDAY_RE.search(lowered) or _TODAY_RE.search(lowered):
        return "day"
    return ""


def _unresolved_time_phrases(text: str) -> list[str]:
    """Temporal phrases the planner detected but could not resolve.

    Returns the matched phrases so ``plan`` can refuse to mark a draft
    ready when the user scoped the question in time and the window got
    dropped. Spans handled by the window resolver (and therefore
    reflected in ``time.start``/``time.end``/``time.range``) are
    subtracted; comparison contexts ("vs last month") are excluded by
    the shared ``_COMPARISON_GUARD`` because the period-shift patterns
    own those.
    """

    lowered = str(text or "").lower()
    resolved_spans: list[tuple[int, int]] = []
    for pattern in (
        _QUARTER_YEAR_RE,
        _MONTH_YEAR_RANGE_RE,
        _MONTH_RANGE_SHARED_YEAR_RE,
        _SINGLE_MONTH_YEAR_RE,
        _RELATIVE_WINDOW_RE,
        _YESTERDAY_RE,
        _TODAY_RE,
        _THIS_PERIOD_RE,
    ):
        for match in pattern.finditer(lowered):
            resolved_spans.append(match.span())
    out: list[str] = []
    for pattern in (_TEMPORAL_CUE_RE, _SINCE_CUE_RE):
        for match in pattern.finditer(lowered):
            start, end = match.span()
            if any(start < r_end and r_start < end for r_start, r_end in resolved_spans):
                continue
            phrase = match.group(0).strip()
            if phrase not in out:
                out.append(phrase)
    return out


# Window forms the planner can resolve from natural language. Surfaced
# verbatim in recovery hints when a temporal phrase fails to resolve.
_SUPPORTED_WINDOW_FORMS = (
    "last N days/weeks/months/quarters/years (e.g. 'last 7 days')",
    "last/past/previous <day|week|month|quarter|year> (e.g. 'last month')",
    "this/current <week|month|quarter|year>",
    "yesterday / today",
    "Q1-Q4 with a year (e.g. 'Q2 2025')",
    "explicit month ranges (e.g. 'January 2017 through June 2017', 'Jan-Jun 2017')",
    "a single month with a year (e.g. 'March 2024')",
    "explicit time.start / time.end ISO dates via partial_query",
)


def _strip_leading_rank_count(raw: str) -> str:
    rank_words = "|".join(re.escape(word) for word in sorted(_NUMBER_WORDS))
    return re.sub(rf"^\s*(?:\d+|{rank_words})\s+", "", raw, count=1).strip()


def _requested_grouping_terms(text: str) -> list[str]:
    lowered = str(text or "").lower()
    top_by_match = re.search(
        r"^\s*top\s+([a-z0-9 _-]+?)\s+by\s+([a-z0-9 _-]+?)(?:[.?!,;]|$)",
        lowered,
    )
    if top_by_match:
        raw_terms = _strip_leading_rank_count(top_by_match.group(1).strip())
    else:
        by_match = re.search(
            r"\bby ([a-z0-9 _-]+?)(?:\s+(?:where|for|from|in|with|during|over|having|who|that)\b|[.?!,;]|$)",
            lowered,
        )
        raw_terms = by_match.group(1).strip() if by_match else ""
    if not raw_terms:
        return []
    return [term.strip() for term in re.split(r"\s*(?:,| and | & )\s*", raw_terms) if term.strip()]


def _is_temporal_grouping_term(term: str) -> bool:
    term_tokens = set(_tokens(term))
    return bool(
        term in {"day", "week", "month", "quarter", "year", "delivered month", "ordered month"}
        or term_tokens
        and term_tokens.issubset(
            {"day", "week", "month", "quarter", "year", "time", "delivered", "ordered"}
        )
    )


def _term_matches_value_domain(config: Any, term: str) -> bool:
    term_tokens = set(_tokens(term))
    if not term_tokens:
        return False
    for domain in config.value_domains:
        for row in list(domain.values or []):
            values = [
                str(row.value),
                str(row.label),
                *[str(alias) for alias in list(row.aliases or [])],
            ]
            for value in values:
                value_tokens = set(_tokens(value))
                if value_tokens and value_tokens.issubset(term_tokens):
                    return True
    return False


def _maybe_group_by(config: Any, text: str, *, target_terms: Iterable[str] = ()) -> list[str]:
    lowered = str(text or "").lower()
    terms = _runtime_composition_terms(text)
    target_set = {term for term in target_terms if term}
    group_by: list[str] = []
    if terms & {"segment", "segments"} and ("customer" in terms or "historical" in terms):
        dim = _object_by_id(config.dimensions, "dimension.jaffle_customer_history_segment")
        if dim is not None:
            group_by.append(dim.id)
    if "store" in terms:
        dim = _dimension(config, ["store", "name"])
        if dim is not None:
            group_by.append(dim.id)
    if any(term in lowered for term in ("geo", "geography", "region", "parent")):
        dim = _dimension(config, ["geo"], prefer_parent="parent" in lowered)
        if dim is not None:
            group_by.append(dim.id)
    for term in _requested_grouping_terms(text):
        term_tokens = set(_tokens(term))
        if (
            term_tokens & {"store", "geo"}
            or _is_temporal_grouping_term(term)
            or _term_matches_value_domain(config, term)
        ):
            continue
        if target_set and term_tokens:
            metric_qualifier_words = {
                "count",
                "volume",
                "sum",
                "total",
                "value",
                "amount",
                "average",
                "mean",
            }
            content_tokens = term_tokens - metric_qualifier_words
            if content_tokens and content_tokens.issubset(target_set):
                continue
            if content_tokens and not (content_tokens - target_set):
                continue
        dim = _dimension(config, term_tokens)
        if dim is not None:
            group_by.append(dim.id)
    return list(dict.fromkeys(group_by))


def _product_filter(config: Any, value: str) -> dict[str, Any] | None:
    dim = _dimension_for_value(config, value, terms=["product"])
    if dim is None:
        return None
    return {"dimension": dim.id, "op": "=", "value": value}


def _metric_predicate(metric: Any, entity: Any) -> dict[str, Any]:
    return {
        "metric": metric.id,
        "entity": entity.id,
        "op": ">",
        "value": 0,
        "time_alignment": "same_query_period",
    }


def _add_order(query: dict[str, Any]) -> None:
    order_by: list[dict[str, str]] = []
    if query.get("time"):
        order_by.append({"field": "time", "direction": "ASC"})
    for dim_id in list(query.get("group_by", []) or []):
        order_by.append({"field": dim_id, "direction": "ASC"})
    if order_by:
        query["order_by"] = order_by


def _runtime_composition_terms(text: str) -> set[str]:
    return set(_tokens(text))


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


def _predicate_metric_terms(text: str, terms: set[str], target_terms: list[str]) -> list[list[str]]:
    """Return predicate-metric term groups for ``qualified_metric_rollup``."""
    phrase_groups = _qualification_phrase_token_groups(text, target_terms)
    if phrase_groups:
        return phrase_groups
    predicate_terms: list[list[str]] = []
    for channel in ("sms", "push", "email"):
        if channel in terms:
            predicate_terms.append([channel])
    if predicate_terms:
        return predicate_terms
    if "order" in terms and "order" not in (target_terms or []):
        predicate_terms.append(["order"])
    if "session" in terms and "session" not in (target_terms or []):
        predicate_terms.append(["session"])
    if predicate_terms:
        return predicate_terms
    if "message" in terms and "message" not in (target_terms or []):
        return [["message"]]
    return []


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


def _predicate_grain(text: str, output_grain: str) -> str:
    lowered = str(text or "").lower()
    for candidate in ("day", "week", "month", "quarter", "year"):
        mentioned = (
            f"that {candidate}" in lowered
            or f"same {candidate}" in lowered
            or f"in the {candidate}" in lowered
            or f"in that {candidate}" in lowered
            or f"per {candidate}" in lowered
        )
        if mentioned and candidate != output_grain:
            return candidate
    return ""


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


_PERIOD_SHIFT_TRIGGERS = (
    (r"\byoy\b", "year"),
    (r"\byear[\s\-]?over[\s\-]?year\b", "year"),
    (r"\bvs\.?\s+(?:last|prior|previous)\s+year\b", "year"),
    (r"\bversus\s+(?:last|prior|previous)\s+year\b", "year"),
    (r"\bcompared\s+to\s+(?:last|prior|previous)\s+year\b", "year"),
    (r"\bmom\b", "month"),
    (r"\bmonth[\s\-]?over[\s\-]?month\b", "month"),
    (r"\bvs\.?\s+(?:last|prior|previous)\s+month\b", "month"),
    (r"\bwow\b", "week"),
    (r"\bweek[\s\-]?over[\s\-]?week\b", "week"),
    (r"\bvs\.?\s+(?:last|prior|previous)\s+week\b", "week"),
    (r"\bqoq\b", "quarter"),
    (r"\bquarter[\s\-]?over[\s\-]?quarter\b", "quarter"),
)


def _period_shift_grain(text: str) -> str:
    lowered = str(text or "").lower()
    for pattern, grain in _PERIOD_SHIFT_TRIGGERS:
        if re.search(pattern, lowered):
            return grain
    return ""


def _top_n_intent(text: str) -> tuple[bool, int]:
    """Return (is_top_intent, n). n is the parsed top-N (defaults to 5).

    Matches both literal "top N" and the looser "which N <noun> …
    highest/most/best/largest/by" phrasing surfaced by blind-agent
    feedback ("which 3 stores have the highest revenue").
    """

    lowered = str(text or "")
    match = _TOP_N_PATTERN.search(lowered)
    if match:
        raw = match.group(1)
        if not raw:
            return True, _DEFAULT_TOP_LIMIT
        try:
            return True, int(raw)
        except ValueError:
            return True, _DEFAULT_TOP_LIMIT
    rank_match = _RANK_PATTERN.search(lowered)
    if rank_match:
        try:
            return True, int(rank_match.group(1))
        except ValueError:
            return True, _DEFAULT_TOP_LIMIT
    return False, _DEFAULT_TOP_LIMIT


def _predicate_input(target: Any) -> dict[str, Any]:
    object_id = str(getattr(target, "id", ""))
    if object_id.startswith("measure."):
        return {"measure": object_id}
    return {"metric": object_id}


_CANONICAL_PREDICATE_MEASURES: dict[str, list[str]] = {
    "order": ["measure.jaffle.order_count"],
    "session": ["measure.jaffle.session_starts"],
    "customer": ["measure.jaffle.ordering_customer_count", "measure.jaffle.customer_count"],
    "revenue": ["measure.jaffle.revenue_usd"],
    "signup": ["measure.jaffle.signup_count"],
}


def _preferred_predicate_target(
    config: Any,
    metric_terms: list[str],
    *,
    target_measure: Any,
) -> Any | None:
    if not metric_terms:
        return None
    term_set = set(metric_terms)
    has_distinct = "distinct" in term_set

    content_tokens = [t for t in metric_terms if t not in _QUALIFICATION_STOP_TOKENS]
    if len(content_tokens) == 1:
        canonical_ids = _CANONICAL_PREDICATE_MEASURES.get(content_tokens[0], [])
        for measure_id in canonical_ids:
            match = next((row for row in config.measures if row.id == measure_id), None)
            if match is not None:
                return match

    qualifier_prefixes = (
        "new_",
        "high_value_",
        "repeat_",
        "visiting_",
        "lifetime_",
        "delivered_",
        "drink_",
        "food_",
        "cumulative_",
    )
    measure_ranked: list[tuple[int, int, int, int, str, Any]] = []
    target_role = str(getattr(target_measure, "default_temporal_role", "") or "")
    for row in config.measures:
        score = _score(row, term_set)
        if score <= 0:
            continue
        role_match = 1 if str(getattr(row, "default_temporal_role", "") or "") == target_role else 0
        distinct_match = (
            1
            if (
                has_distinct
                and str(getattr(row, "measure_class", "") or "") == "distinct_population"
            )
            else 0
        )
        base_id = getattr(row, "id", "").split(".", 2)[-1] if getattr(row, "id", "") else ""
        unqualified_bonus = 1
        for prefix in qualifier_prefixes:
            if base_id.startswith(prefix):
                prefix_token = prefix.rstrip("_").split("_", 1)[0]
                if prefix_token not in term_set:
                    unqualified_bonus = 0
                    break
        measure_ranked.append(
            (-score, -unqualified_bonus, -role_match, -distinct_match, getattr(row, "id", ""), row)
        )
    measure_ranked.sort()

    metric_ranked: list[tuple[int, str, Any]] = []
    for row in config.metric_recipes:
        score = _score(row, term_set)
        if score <= 0:
            continue
        metric_ranked.append((-score, getattr(row, "id", ""), row))
    metric_ranked.sort()

    if measure_ranked:
        return measure_ranked[0][-1]
    if metric_ranked:
        return metric_ranked[0][-1]
    return _preferred_metric(config, term_set)
