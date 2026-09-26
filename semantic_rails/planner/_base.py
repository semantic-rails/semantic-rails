"""Shared helpers for intent patterns.

Patterns under ``semantic_rails/planner/patterns/`` import what they
need from this module so the orchestrator stays thin.
"""

from __future__ import annotations

import copy
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


def _named_metric(config: Any, text: str) -> tuple[Any, str] | None:
    """The metric the question names by its label, an alias or its id, and the
    question with that name replaced by the id.

    The name has two words or more, and every measure the question names lies
    inside it, so the metric is the more specific reading: "completed revenue
    by month" means the Completed Revenue metric, not the Revenue measure. So
    does a measure with the same name ("rolling 28-day revenue" is both). A
    measure named elsewhere ("revenue and orders") leaves the question to
    measure-first resolution. The id stands in for the
    name so its words ("revenue, trailing 7 days") aren't read again as a
    window, a count or a value.
    """

    words = list(re.finditer(r"[^\W_]+", text.lower()))
    said = [word.group() for word in words]

    def named(rows: Iterable[Any]) -> Iterable[tuple[int, int, Any]]:
        for row in rows:
            for name in (row.label, row.id, *(getattr(row, "aliases", None) or [])):
                parts = re.findall(r"[^\W_]+", str(name or "").lower())
                for start in range(len(said) - len(parts) + 1):
                    if parts and said[start : start + len(parts)] == parts:
                        yield len(parts), start, row

    size, start, metric = max(
        (item for item in named(config.metric_recipes) if item[0] > 1),
        key=lambda item: item[0],
        default=(0, 0, None),
    )
    if metric is None or any(
        not (start <= begin and begin + length <= start + size)
        for length, begin, _row in named(config.measures)
    ):
        return None
    first, last = words[start].start(), words[start + size - 1].end()
    return metric, f"{text[:first]}{metric.id}{text[last:]}"


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


def _explicit_grain(text: str, clock: str = "") -> str:
    """Return the grain the intent explicitly cues ("" when absent).

    With ``clock``, the label of the query's temporal role, a grouping that names that clock
    ("by order month", "by order date") takes its own unit, else a cadence the question
    names ("monthly", "per week", "at week grain"), else days. A unit that only a window
    names ("for the first quarter of 2017", "last month") doesn't set it.
    """
    lowered = str(text or "").lower()
    terms = _runtime_composition_terms(text)
    for term in _requested_grouping_terms(text) if clock else ():
        if _names_time_axis(term, clock):
            tokens = set(_tokens(term))
            cues = ("by {}", "per {}", "each {}", "{} grain", "{}ly")
            own = [unit for unit in _TIME_UNITS if unit in tokens]
            cadence = [
                unit
                for unit in _TIME_UNITS
                if any(cue.format(unit) in lowered for cue in cues)
                or (unit == "day" and "daily" in lowered)
            ]
            return (own or cadence or ["day"])[0]
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


def _single_bucket_grain(bounds: dict[str, Any]) -> str:
    """The finest calendar grain that holds the whole window in one bucket, or ``""``."""

    try:
        start = date.fromisoformat(str(bounds["start"])[:10])
        last = date.fromisoformat(str(bounds["end"])[:10]) - timedelta(days=1)
    except (KeyError, ValueError):
        return ""
    if last < start:
        return ""
    if start.year != last.year:
        # Whole calendar years ("in 2016 and 2017") read as one total per year.
        whole_years = (start.month, start.day, last.month, last.day) == (1, 1, 12, 31)
        return "year" if whole_years else ""
    if start == last:
        return "day"
    if start.weekday() == 0 and last == start + timedelta(days=6):
        return "week"
    if start.month == last.month:
        return "month"
    if (start.month - 1) // 3 == (last.month - 1) // 3:
        return "quarter"
    return "year"


def _time_spec(role: str, text: str, clock: str = "") -> dict[str, Any]:
    lowered = str(text or "").lower()
    grain = _explicit_grain(text, clock)
    window = _time_window(text)
    if not grain and window.relative_unit:
        # A relative window ("last 7 days", "yesterday") buckets at its own
        # unit instead of the generic month default.
        grain = window.relative_unit
    if not grain and not _TREND_CUE_RE.search(lowered):
        # A total over a calendar window ("revenue in 2017", "orders in the
        # first half of 2017") needs a grain that yields one bucket.
        grain = _single_bucket_grain(window.bounds)
    return {"temporal_role": role, "grain": grain or "month", **window.bounds}


def _fiscal_calendar(config: Any) -> Any | None:
    """The package's one non-default calendar that names itself fiscal, else ``None``."""

    rows = [
        row
        for row in config.entities
        if row.kind == "time"
        and (row.calendar_id or "default") != "default"
        and "fiscal" in _tokens(f"{row.calendar_id} {row.id} {row.name} {row.label}")
    ]
    return rows[0] if len(rows) == 1 else None


# The only fiscal mention plan honors itself: a bucket ("by fiscal quarter", "fiscal
# quarterly"). Any other ("the first fiscal quarter", "since the start of the fiscal year",
# "vs prior fiscal year") scopes or compares time in a way the draft doesn't carry.
_FISCAL_BUCKET_RE = re.compile(
    r"\b(?:by|per|each)\s+fiscal[\s-]+(?P<unit>year|quarter|month|week)s?\b"
    r"|\bfiscal[\s-]+(?P<cadence>year|quarter|month|week)ly\b"
    r"|\bfiscal[\s-]+annual\b"
)
# Period-to-date resets on Gregorian periods whatever the calendar, and plan drops a
# to-date or rolling ask.
_TO_DATE_OR_ROLLING_RE = re.compile(r"\b(?:[ymqw]td|to[\s-]+date|rolling|trailing|moving)\b")
# The calendar column a filled series buckets each grain on.
_CALENDAR_BUCKET_COLUMNS = {
    "day": "date_day",
    "week": "week_start",
    "month": "month_start",
    "quarter": "quarter_start",
    "year": "year_start",
}


def _with_fiscal_calendar(config: Any, text: str, query: dict[str, Any]) -> dict[str, Any]:
    """Bucket a fiscal question's draft on the package's fiscal calendar.

    Only when every fiscal mention asks for fiscal buckets of the draft's grain, and
    nothing asks for a to-date or rolling value. ``fill`` routes the buckets through
    the calendar (the engine refuses a non-default ``calendar_id`` without it). A
    ``group_by`` on the calendar's bucket for the same grain ("by fiscal quarter" read
    as a dimension) is what the time bucket now holds, so it goes, and an ``order_by``
    on it orders by time. Otherwise the draft is unchanged and plan reports the gap.
    """

    time = query.get("time")
    calendar = _fiscal_calendar(config)
    lowered = str(text or "").lower()
    units = {
        match["unit"] or match["cadence"] or "year" for match in _FISCAL_BUCKET_RE.finditer(lowered)
    }
    if (
        calendar is None
        or not isinstance(time, dict)
        or time.get("calendar_id")
        # The draft's grain is the one bucket the question names, not one plan chose to
        # hold a window ("fiscal annual revenue from 2017-02-01 to 2017-02-28": month).
        or units != {time.get("grain")}
        or _FISCAL_RE.search(_FISCAL_BUCKET_RE.sub(" ", lowered))
        or _TO_DATE_OR_ROLLING_RE.search(lowered)
    ):
        return query
    column = _CALENDAR_BUCKET_COLUMNS.get(str(time["grain"]))
    bucket = {
        row.id for row in config.dimensions if row.entity == calendar.id and row.column == column
    }
    out = {**query, "time": {**time, "calendar_id": calendar.calendar_id, "fill": True}}
    order_by: list[Any] = []
    for item in query.get("order_by") or []:
        if isinstance(item, dict) and item.get("field") in bucket:
            item = {**item, "field": "time"}
        if item not in order_by:
            order_by.append(item)
    kept = {
        "group_by": [item for item in query.get("group_by") or [] if item not in bucket],
        "order_by": order_by,
    }
    for key, items in kept.items():
        if items:
            out[key] = items
        else:
            out.pop(key, None)
    return out


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
    rf"(?:(\d+|{_NUMBER_WORD_ALT})\s+)?({_TIME_UNIT_ALT})s?\b(?!\s+of\b)"
)
_YESTERDAY_RE = re.compile(rf"{_COMPARISON_GUARD}\byesterday\b")
_TODAY_RE = re.compile(rf"{_COMPARISON_GUARD}\btoday\b")
_THIS_PERIOD_RE = re.compile(rf"{_COMPARISON_GUARD}\b(?:current|this)\s+({_TIME_UNIT_ALT})\b")

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
_ORDINALS = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
}

# Calendar phrases the planner resolves. Month names only count next to a
# year, so bare "may"/"march" verbs never resolve as dates. A spoken end day
# is inclusive; the Query IR end is exclusive.
_YEAR = r"(20\d{2})"
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
_MONTH = rf"({_MONTH_ALT})\.?"
_ISO_DATE = r"(20\d{2})-(\d{2})-(\d{2})"
_RANGE_START = r"(?:(?:from|between)\s+)?"
_RANGE_CONNECTOR = r"(?:through|thru|until|till|to|and|[-–—])"
_ISO_RANGE_RE = re.compile(
    rf"\b{_RANGE_START}{_ISO_DATE}\s*(?:through|thru|until|till|to|and|[-–—])\s*{_ISO_DATE}\b"
)
_ISO_DAY_RE = re.compile(rf"\b(?:on\s+)?{_ISO_DATE}\b")
_DAY_RANGE_RE = re.compile(
    rf"\b{_RANGE_START}{_MONTH}\s+{_DAY}(?:,?\s+{_YEAR})?\s*{_RANGE_CONNECTOR}\s*"
    rf"(?:{_MONTH}\s+)?{_DAY}(?:,?\s+{_YEAR})?\b"
)
_MONTH_DAY_RE = re.compile(rf"\b(?:on\s+)?{_MONTH}\s+{_DAY},?\s+{_YEAR}\b")
_DAY_MONTH_RE = re.compile(rf"\b(?:on\s+)?(?:the\s+)?{_DAY}\s+(?:of\s+)?{_MONTH},?\s+{_YEAR}\b")
_MONTH_YEAR_RANGE_RE = re.compile(
    rf"\b{_RANGE_START}{_MONTH}\s+{_YEAR}\s*{_RANGE_CONNECTOR}\s*{_MONTH}\s+{_YEAR}\b"
)
_MONTH_RANGE_SHARED_YEAR_RE = re.compile(
    rf"\b{_RANGE_START}{_MONTH}\s*{_RANGE_CONNECTOR}\s*{_MONTH}\s+(?:of\s+)?{_YEAR}\b"
)
_MONTH_YEAR_RE = re.compile(rf"\b{_MONTH}\s+(?:of\s+)?{_YEAR}\b")
_QUARTER_YEAR_RE = re.compile(rf"\bq([1-4])\s*(?:of\s+)?['-]?\s*{_YEAR}\b")
_YEAR_QUARTER_RE = re.compile(rf"\b{_YEAR}\s*-?\s*q([1-4])\b")
_ORDINAL_QUARTER_RE = re.compile(
    rf"\b(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter\s+(?:of\s+)?{_YEAR}\b"
)
_HALF_YEAR_RE = re.compile(rf"\b(?:(first|second|1st|2nd)\s+half\s+(?:of\s+)?|h([12])\s*){_YEAR}\b")
_YEAR_HALF_RE = re.compile(rf"\b{_YEAR}\s*-?\s*h([12])\b")
# Several calendar years: "between 2016 and 2017", "2016 through 2017",
# "in 2016 and 2017". "and" joins only consecutive years; "2015 and 2017"
# doesn't mean 2016 too.
_YEAR_SPAN_RE = re.compile(
    rf"\b(?:(?:in|for|during|throughout|over|across|between|from)\s+)?{_YEAR}\s*"
    rf"(and|through|thru|until|till|to|[-–—])\s*{_YEAR}\b"
)
# A single calendar year resolves only after a preposition that scopes a
# window: "in 2017", "for 2017", "during 2017", "the year 2017". Anything
# else ("2017 revenue", "early 2017", "end of 2017", "2000 customers") is
# reported, never guessed.
_YEAR_IN_RE = re.compile(
    rf"\b(?:(?:in|for|during|throughout|within)\s+(?:the\s+)?(?:(?:calendar\s+)?year\s+)?"
    rf"|the\s+(?:calendar\s+)?year\s+){_YEAR}\b"
)

# A calendar phrase directly after one of these is a bound or a comparison,
# not a window ("before 2017", "since March 2017", "as of June 30, 2017",
# "2017 vs 2016"), and a phrase after "of" is qualified ("the end of
# 2017", "the week of April 3, 2017"). The planner reports these.
_BOUNDARY_BEFORE_RE = re.compile(
    r"(?:\b(?:before|after|since|until|till|through|thru|by|from|ending|starting|beginning|"
    r"prior\s+to|up\s+to|as\s+of|earlier\s+than|later\s+than|pre|post|vs\.?|versus|"
    r"compared\s+(?:to|with)|relative\s+to|against|over|than|of)[\s-]+(?:the\s+)?)$"
)
# The tail or head of a range the resolver couldn't parse: "1-7 April 2017",
# "from Jan 1 2017 to Mar 2017".
_UNPARSED_RANGE_BEFORE_RE = re.compile(
    rf"(?:\d(?:st|nd|rd|th)?|\b(?:{_MONTH_ALT}))\.?,?\s*"
    rf"(?:[-–—&]|\b(?:to|through|thru|until|till|and|or)\b)\s*$"
)
# After a calendar phrase: the tail of a range ("... to Mar 2017"), a month
# after its year ("2017 March"), or a fiscal-year suffix ("2017/18").
_UNPARSED_RANGE_AFTER_RE = re.compile(
    rf"^\s*(?:(?:[-–—]|\b(?:to|through|thru|until|till)\b)\s*"
    rf"(?:\d|\b(?:{_MONTH_ALT})\b|today\b|now\b)|/\s*\d{{2}}\b|(?:{_MONTH_ALT})\b)"
)
# "revenue in 2017 over 2016", "more revenue in 2017 than in 2016": a
# comparison of two years, even where "over 2000" alone would be a quantity.
_YEAR_COMPARISON_RE = re.compile(
    r"\b20\d{2}\s+(?:over|above|below|under|than|exceeding|versus|vs\.?|against|"
    r"compared\s+(?:to|with)|relative\s+to)\s+(?:in\s+|the\s+)?(?:year\s+)?20\d{2}\b"
)
# Range forms whose connector may be "and": "and" makes a range only after
# "between" ("between March and May 2017"); "March and May 2017" names two
# periods, which the planner reports rather than spanning.
_AND_JOINABLE_FORMS: tuple[re.Pattern[str], ...] = (
    _ISO_RANGE_RE,
    _DAY_RANGE_RE,
    _MONTH_YEAR_RANGE_RE,
    _MONTH_RANGE_SHARED_YEAR_RE,
)

# Cues that ask for a series over time even without a named grain.
_TREND_CUE_RE = re.compile(r"\b(?:over time|trends?|trending|history|historical|time series)\b")

# Cues to a time scope the planner can't resolve ("last few weeks", "since
# 2023", "4/3/2017", "in March"). Whatever of these no resolved window
# covers is reported instead of guessed.
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
_OTHER_TIME_CUE_RES = (
    _TEMPORAL_CUE_RE,
    _SINCE_CUE_RE,
    # Numeric dates are ambiguous between month/day and day/month orders.
    re.compile(r"\b\d{1,2}/\d{1,2}/(?:20)?\d{2}\b"),
    # A month named without a year ("in March", "April 3").
    re.compile(
        rf"\b(?:in|for|during|since|before|after|until|till|through|by|from|of|on)\s+"
        rf"(?:early\s+|late\s+|mid-?\s*)?(?:{_MONTH_ALT})\b(?!\.?\s*,?\s*(?:\d|of\s+20))"
    ),
    re.compile(rf"\b(?:{_MONTH_ALT})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b(?!\s*,?\s*(?:20\d{{2}}|\d))"),
    # A quarter or half named without a year ("in Q2", "the second half").
    re.compile(
        r"\b(?:q[1-4]|h[12])\b|\b(?:first|second|third|fourth|1st|2nd|3rd|4th)\s+(?:quarter|half)\b"
    ),
    # A fiscal period ("last fiscal quarter", "this fiscal year", "FY2017"):
    # its dates come from the package's fiscal calendar.
    re.compile(
        rf"{_COMPARISON_GUARD}\b(?:last|past|previous|prior|trailing|this|current|next)\s+"
        rf"(?:(?:\d+|{_NUMBER_WORD_ALT})\s+)?fiscal\s+[a-z]+"
    ),
    re.compile(r"\bfy\s*'?\d{2}(?:\d{2})?\b"),
)
# "fiscal quarter", "FY2017": the question counts time on a fiscal calendar,
# so a window resolves only from exact days, never as the Gregorian period of
# the same name ("fiscal Q2 2017" is not April to June).
_FISCAL_RE = re.compile(r"\bfiscal\b|\bfy(?:\s*'?\d{2}(?:\d{2})?)?\b")
_DAY_EXACT_FORMS = (_ISO_RANGE_RE, _ISO_DAY_RE, _DAY_RANGE_RE, _MONTH_DAY_RE, _DAY_MONTH_RE)
# A 20xx number that counts rather than dates ("top 2000 customers", "$2000",
# "2000 or more orders") is not a time cue.
_QUANTITY_BEFORE_RE = re.compile(
    r"(?:[$#><=≥≤]\s*|\b(?:top|bottom|first|over|under|above|below|least|most|than|"
    r"exceeding|exceeds|exceed|store|number|id|no\.?)\s+)$"
)
_QUANTITY_AFTER_RE = re.compile(r"^\s*(?:\+|%|percent\b|or\s+(?:more|fewer|less)\b)")
_YEAR_TOKEN_RE = re.compile(r"\b20\d{2}\b")


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


def _current_period_bounds(unit: str, today: date) -> dict[str, str]:
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


def _day_window(first: date, last: date) -> dict[str, str]:
    if last < first:
        return {}
    return {"start": first.isoformat(), "end": (last + timedelta(days=1)).isoformat()}


def _iso_range_bounds(match: re.Match[str]) -> dict[str, str]:
    y1, m1, d1, y2, m2, d2 = (int(group) for group in match.groups())
    return _day_window(date(y1, m1, d1), date(y2, m2, d2))


def _iso_day_bounds(match: re.Match[str]) -> dict[str, str]:
    year, month, day = (int(group) for group in match.groups())
    return _day_window(date(year, month, day), date(year, month, day))


def _day_range_bounds(match: re.Match[str]) -> dict[str, str]:
    first_month, first_day, first_year, last_month, last_day, last_year = match.groups()
    if not first_year and not last_year:
        return {}
    first = date(int(first_year or last_year), _MONTH_NUMBERS[first_month], int(first_day))
    last = date(
        int(last_year or first_year),
        _MONTH_NUMBERS[last_month or first_month],
        int(last_day),
    )
    return _day_window(first, last)


def _month_day_bounds(match: re.Match[str]) -> dict[str, str]:
    month, day, year = match.groups()
    day_date = date(int(year), _MONTH_NUMBERS[month], int(day))
    return _day_window(day_date, day_date)


def _day_month_bounds(match: re.Match[str]) -> dict[str, str]:
    day, month, year = match.groups()
    day_date = date(int(year), _MONTH_NUMBERS[month], int(day))
    return _day_window(day_date, day_date)


def _month_year_range_bounds(match: re.Match[str]) -> dict[str, str]:
    start_month, start_year, end_month, end_year = match.groups()
    start = _month_start(int(start_year), _MONTH_NUMBERS[start_month])
    end = _month_end_exclusive(int(end_year), _MONTH_NUMBERS[end_month])
    return {"start": start, "end": end} if start < end else {}


def _shared_year_month_range_bounds(match: re.Match[str]) -> dict[str, str]:
    start_month, end_month, year = match.groups()
    start = _month_start(int(year), _MONTH_NUMBERS[start_month])
    end = _month_end_exclusive(int(year), _MONTH_NUMBERS[end_month])
    return {"start": start, "end": end} if start < end else {}


def _month_year_bounds(match: re.Match[str]) -> dict[str, str]:
    month, year = match.groups()
    return {
        "start": _month_start(int(year), _MONTH_NUMBERS[month]),
        "end": _month_end_exclusive(int(year), _MONTH_NUMBERS[month]),
    }


def _quarter_window(quarter: int, year: int) -> dict[str, str]:
    start_month = (quarter - 1) * 3 + 1
    end_year, end_month = (year + 1, 1) if quarter == 4 else (year, start_month + 3)
    return {"start": _month_start(year, start_month), "end": _month_start(end_year, end_month)}


def _quarter_year_bounds(match: re.Match[str]) -> dict[str, str]:
    return _quarter_window(int(match.group(1)), int(match.group(2)))


def _year_quarter_bounds(match: re.Match[str]) -> dict[str, str]:
    return _quarter_window(int(match.group(2)), int(match.group(1)))


def _ordinal_quarter_bounds(match: re.Match[str]) -> dict[str, str]:
    return _quarter_window(_ORDINALS[match.group(1)], int(match.group(2)))


def _half_window(half: int, year: int) -> dict[str, str]:
    if half == 1:
        return {"start": f"{year:04d}-01-01", "end": f"{year:04d}-07-01"}
    return {"start": f"{year:04d}-07-01", "end": f"{year + 1:04d}-01-01"}


def _half_year_bounds(match: re.Match[str]) -> dict[str, str]:
    ordinal, number, year = match.groups()
    half = int(number) if number else _ORDINALS[ordinal]
    return _half_window(half, int(year))


def _year_half_bounds(match: re.Match[str]) -> dict[str, str]:
    return _half_window(int(match.group(2)), int(match.group(1)))


def _year_span_bounds(match: re.Match[str]) -> dict[str, str]:
    first, connector, last = int(match.group(1)), match.group(2), int(match.group(3))
    if last <= first or (connector == "and" and last != first + 1):
        return {}
    return {"start": f"{first:04d}-01-01", "end": f"{last + 1:04d}-01-01"}


def _year_in_bounds(match: re.Match[str]) -> dict[str, str]:
    year = int(match.group(1))
    return {"start": f"{year:04d}-01-01", "end": f"{year + 1:04d}-01-01"}


# Calendar forms, most specific first, so "April 7, 2017" never widens to its
# month or year. Each span of text resolves through at most one form.
_CALENDAR_FORMS: tuple[tuple[re.Pattern[str], Any], ...] = (
    (_ISO_RANGE_RE, _iso_range_bounds),
    (_ISO_DAY_RE, _iso_day_bounds),
    (_DAY_RANGE_RE, _day_range_bounds),
    (_MONTH_DAY_RE, _month_day_bounds),
    (_DAY_MONTH_RE, _day_month_bounds),
    (_MONTH_YEAR_RANGE_RE, _month_year_range_bounds),
    (_MONTH_RANGE_SHARED_YEAR_RE, _shared_year_month_range_bounds),
    (_MONTH_YEAR_RE, _month_year_bounds),
    (_QUARTER_YEAR_RE, _quarter_year_bounds),
    (_YEAR_QUARTER_RE, _year_quarter_bounds),
    (_ORDINAL_QUARTER_RE, _ordinal_quarter_bounds),
    (_HALF_YEAR_RE, _half_year_bounds),
    (_YEAR_HALF_RE, _year_half_bounds),
    (_YEAR_SPAN_RE, _year_span_bounds),
    (_YEAR_IN_RE, _year_in_bounds),
)


@dataclass(frozen=True)
class _TimeWindow:
    """What a question says about time: its window, or what couldn't be resolved."""

    bounds: dict[str, Any] = field(default_factory=dict)
    relative_unit: str = ""
    unresolved: tuple[str, ...] = ()
    # Every span of the lowercased text read as time, resolved or not.
    spans: tuple[tuple[int, int], ...] = ()


def _overlaps(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in spans)


def _calendar_windows(
    lowered: str,
) -> tuple[list[tuple[tuple[int, int], dict[str, str]]], list[tuple[int, int]]]:
    """Resolved calendar spans with their bounds, and calendar spans rejected as bounds."""

    accepted: list[tuple[tuple[int, int], dict[str, str]]] = []
    rejected: list[tuple[int, int]] = []
    fiscal = _FISCAL_RE.search(lowered) is not None
    for pattern, to_bounds in _CALENDAR_FORMS:
        for match in pattern.finditer(lowered):
            span = match.span()
            if _overlaps(span, [row[0] for row in accepted] + rejected):
                continue
            before, after = lowered[: span[0]], lowered[span[1] :]
            try:
                bounds = to_bounds(match)
            except (KeyError, ValueError):
                bounds = {}
            text = match.group(0)
            joined = pattern in _AND_JOINABLE_FORMS and re.search(r"\band\b", text)
            if joined and not text.startswith("between"):
                bounds = {}
            boundary = _BOUNDARY_BEFORE_RE.search(before)
            if boundary:
                # Report the bound with its word: "since march 2017".
                span = (boundary.start(), span[1])
            if (
                not bounds
                or boundary
                or (fiscal and pattern not in _DAY_EXACT_FORMS)
                or _UNPARSED_RANGE_BEFORE_RE.search(before)
                or _UNPARSED_RANGE_AFTER_RE.search(after)
            ):
                rejected.append(span)
                continue
            accepted.append((span, bounds))
    return accepted, rejected


def _relative_window(
    lowered: str, today: date
) -> list[tuple[tuple[int, int], dict[str, Any], str]]:
    """Every relative window in the text: (span, bounds, unit)."""

    candidates: list[tuple[tuple[int, int], dict[str, Any], str]] = []
    for match in _RELATIVE_WINDOW_RE.finditer(lowered):
        unit = match.group(2)
        bounds = {
            "range": {"last": {"unit": unit, "value": _relative_window_value(match.group(1))}}
        }
        candidates.append((match.span(), bounds, unit))
    for match in _YESTERDAY_RE.finditer(lowered):
        # ``range.last`` floors ``end`` to the start of the current period,
        # so {day, 1} is exactly yesterday (end-exclusive).
        candidates.append((match.span(), {"range": {"last": {"unit": "day", "value": 1}}}, "day"))
    for match in _THIS_PERIOD_RE.finditer(lowered):
        candidates.append((match.span(), _current_period_bounds(match.group(1), today), ""))
    for match in _TODAY_RE.finditer(lowered):
        candidates.append((match.span(), _current_period_bounds("day", today), "day"))
    return sorted(candidates, key=lambda row: row[0][0])


def _time_cues(lowered: str) -> list[tuple[int, int]]:
    """Every span that scopes the question in time, resolvable or not."""

    spans = [match.span() for pattern in _OTHER_TIME_CUE_RES for match in pattern.finditer(lowered)]
    for match in _YEAR_TOKEN_RE.finditer(lowered):
        before, after = lowered[: match.start()], lowered[match.end() :]
        if _QUANTITY_BEFORE_RE.search(before) or _QUANTITY_AFTER_RE.search(after):
            continue
        spans.append(match.span())
    return spans


def _phrase(lowered: str, span: tuple[int, int]) -> str:
    """The reported text of a cue; a bare year keeps the word before it ("early 2017")."""

    text = lowered[span[0] : span[1]].strip()
    if _YEAR_TOKEN_RE.fullmatch(text):
        boundary = _BOUNDARY_BEFORE_RE.search(lowered[: span[0]])
        if boundary:
            return lowered[boundary.start() : span[1]].strip()
        lead = lowered[: span[0]].split()[-1:]
        if lead:
            text = f"{lead[0]}{text}" if lead[0].endswith("-") else f"{lead[0]} {text}"
    return text


def _time_window(text: str) -> _TimeWindow:
    """Resolve the question's time window, or report why it can't be resolved.

    A window resolves only when the question names exactly one calendar or
    relative window, in a form the planner reads unambiguously, and no other
    time cue remains. Anything else (a bound such as "before 2017", a
    qualifier such as "the end of 2017", a comparison year, a numeric date,
    two windows at once) is reported as unresolved and the window is left
    unset, never narrowed or widened to the nearest form that parses.
    """

    # "today" and "this month" depend on the date, so it is part of the cache
    # key; each caller gets its own copy, so a draft can't edit the cache.
    text = str(text or "")
    if len(text) > _MAX_TIME_TEXT:
        # A prefix is not the complete question: a suffix can restrict or
        # contradict its window. The plan honesty gate reports this limit.
        return _TimeWindow()
    lowered = text.lower()
    return copy.deepcopy(_resolved_time_window(lowered, date.today()))


# Longer questions are left unresolved; the resolver's cost grows with the
# square of the text. Never resolve only a prefix.
_MAX_TIME_TEXT = 2000


@lru_cache(maxsize=512)
def _resolved_time_window(lowered: str, today: date) -> _TimeWindow:
    accepted, rejected = _calendar_windows(lowered)
    relative = _relative_window(lowered, today)
    if _FISCAL_RE.search(lowered):
        # A fiscal question's "last quarter" or "this year" is a fiscal period.
        rejected += [row[0] for row in relative if row[2] != "day"]
        relative = [row for row in relative if row[2] == "day"]
    windows: list[tuple[tuple[int, int], dict[str, Any], str]] = [
        (span, bounds, "") for span, bounds in accepted
    ]
    for row in relative:
        if not _overlaps(row[0], [item[0] for item in windows]):
            windows.append(row)
    covered = [row[0] for row in windows]
    unresolved_spans = [span for span in rejected if not _overlaps(span, covered)]
    # Two years compared ("2017 over 2016") are reported, whatever resolved.
    unresolved_spans += [match.span() for match in _YEAR_COMPARISON_RE.finditer(lowered)]
    # Longest cues first, so a year inside "4/3/2017" isn't reported twice.
    for span in sorted(_time_cues(lowered), key=lambda item: item[0] - item[1]):
        if not _overlaps(span, covered + unresolved_spans):
            unresolved_spans.append(span)
    time_spans = tuple(sorted(covered + unresolved_spans))
    if unresolved_spans or len(windows) > 1:
        # Report every time phrase, resolved or not: resolving part of an
        # ambiguous question would answer a different one.
        spans = sorted(unresolved_spans + (covered if len(windows) > 1 else []))
        phrases = list(dict.fromkeys(_phrase(lowered, span) for span in spans))
        return _TimeWindow(unresolved=tuple(phrases), spans=time_spans)
    if not windows:
        return _TimeWindow()
    _span, bounds, unit = windows[0]
    return _TimeWindow(bounds=dict(bounds), relative_unit=unit, spans=time_spans)


def _time_bounds_from_text(text: str) -> dict[str, Any]:
    return dict(_time_window(text).bounds)


def _implied_window_grain(lowered: str) -> str:
    """Grain implied by a resolved relative window, or ``""``.

    "last 7 days" implies a daily series; "yesterday"/"today" imply a
    single day bucket.
    """

    return _time_window(lowered).relative_unit


def _unresolved_time_phrases(text: str) -> list[str]:
    """Time phrases the planner detected but did not resolve into a window.

    ``plan`` reports them (``TIME_WINDOW_UNRESOLVED``) instead of marking a
    draft ready, because the draft doesn't carry the window the question
    asked for.
    """

    return list(_time_window(text).unresolved)


# Window forms the planner can resolve from natural language. Surfaced
# verbatim in recovery hints when a temporal phrase fails to resolve.
_SUPPORTED_WINDOW_FORMS = (
    "last N days/weeks/months/quarters/years (e.g. 'last 7 days')",
    "last/past/previous <day|week|month|quarter|year> (e.g. 'last month')",
    "this/current <week|month|quarter|year>",
    "yesterday / today",
    "a calendar year after in/for/during (e.g. 'in 2017'), or consecutive years ('2016 and 2017')",
    "a quarter or half with a year (e.g. 'Q2 2017', 'second quarter of 2017', 'H1 2017')",
    "a month with a year, or a month range (e.g. 'March 2017', 'January 2017 through June 2017')",
    "days with a year, or ISO dates (e.g. 'April 3, 2017', 'April 1 to April 7, 2017', "
    "'2017-04-03')",
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
    parts = re.split(r"\s*(?:,| and | & | by )\s*", raw_terms)
    return [term.strip() for term in parts if term.strip()]


# Words that make a grouping term name a clock ("order date", "order month at month grain").
_TIME_AXIS_WORDS = frozenset({"date", "dates", *_TIME_UNITS})
_GRAIN_WORDS = frozenset({"at", "grain", "level"})


def _names_time_axis(term: str, clock: str) -> bool:
    """Whether a grouping term names the query's own clock, by its label: "order date" for
    "Order time".

    The query's ``time`` block already groups by that clock, at the intent's grain. Resolving
    the term to a dimension instead would group by some other timestamp ("Customer first
    order at"). A term that names another clock ("customer first order date") is not this one.
    """

    tokens = set(_tokens(term))
    content = tokens - _TIME_AXIS_WORDS - _GRAIN_WORDS
    if not clock or not content or not tokens & _TIME_AXIS_WORDS:
        return False
    return content <= set(_tokens(clock))


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


def _maybe_group_by(
    config: Any, text: str, *, target_terms: Iterable[str] = (), clock: str = ""
) -> list[str]:
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
            or _names_time_axis(term, clock)
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
