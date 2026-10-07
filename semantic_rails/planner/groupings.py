from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from ._base import (
    _NAME_CONNECTORS,
    _NUMBER_WORDS,
    _dimension,
    _last_token,
    _object_by_id,
    _runtime_composition_terms,
    _tokens,
)
from .time_phrases import _TIME_UNITS, _names_time_axis
from .time_windows import _time_window
from .visibility import visible_dimensions, visible_value_domains


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


# Cues that ask for a series over time even without a named grain.
_TREND_CUE_RE = re.compile(r"\b(?:over time|trends?|trending|history|historical|time series)\b")


def _strip_leading_rank_count(raw: str) -> str:
    rank_words = "|".join(re.escape(word) for word in sorted(_NUMBER_WORDS))
    return re.sub(rf"^\s*(?:\d+|{rank_words})\s+", "", raw, count=1).strip()


def _name_forms(words: Iterable[str]) -> set[str]:
    """The words with their regular plurals, which name the same object; synonyms do not."""

    words = set(words)
    return (
        words
        | {word + "s" for word in words}
        | {word[:-1] + "ies" for word in words if word.endswith("y")}
        | {
            word + "es"
            for word in words
            if len(word) > 2 and word.endswith(("s", "x", "z", "ch", "sh"))
        }
    )


def _grouping_matches(term: str, row: Any, *, entity: bool = False) -> bool:
    """Match content words to declared names, never substring scores or synonyms.

    A dimension's own words are its label, its aliases and the last part of its name; the
    namespace, model and entity prefix in its id are not its words.
    """

    names = (
        [row.name, row.label]
        if entity
        else [_last_token(row.name), row.label, *(row.aliases or [])]
    )
    words = _name_forms(re.findall(r"[^\W_]+", " ".join(names).lower()))
    content = set(re.findall(r"[^\W_]+", term.lower())) - _NAME_CONNECTORS
    return bool(content and content <= words)


def _requested_grouping_terms(text: str) -> list[str]:
    lowered = str(text or "").lower()
    return [lowered[start:end] for start, end in _requested_grouping_spans(text)]


def _requested_grouping_spans(text: str) -> list[tuple[int, int]]:
    """Record exactly where the existing grouping parser reads each term."""

    lowered = str(text or "").lower()
    top_by_match = re.search(
        r"^\s*top\s+([a-z0-9 _-]+?)\s+by\s+([a-z0-9 _-]+?)(?:[.?!,;]|$)",
        lowered,
    )
    match: re.Match[str] | None
    if top_by_match:
        match = top_by_match
        raw_terms = _strip_leading_rank_count(match.group(1).strip())
    else:
        match = re.search(
            r"\bby ([a-z0-9 _-]+?)(?:\s+(?:where|for|from|in|with|during|over|having|who|that)\b|[.?!,;]|$)",
            lowered,
        )
        raw_terms = match.group(1).strip() if match else ""
    if not match or not raw_terms:
        return []
    offset = match.start(1) + match.group(1).find(raw_terms)
    spans: list[tuple[int, int]] = []
    start = 0
    cuts = [
        (part.start(), part.end()) for part in re.finditer(r"\s*(?:,| and | & | by )\s*", raw_terms)
    ]
    for end, next_start in [*cuts, (len(raw_terms), len(raw_terms))]:
        term = raw_terms[start:end]
        if term.strip():
            low = start + len(term) - len(term.lstrip())
            spans.append((offset + low, offset + low + len(term.strip())))
        start = next_start
    return spans


def _listed_grouping_terms(text: str, config: Any) -> list[str]:
    """Every grouping the question lists, read only to decide whether a draft is ready.

    A draft reads its groupings with ``_requested_grouping_terms``, where a comma ends the
    list. Here the piece after a comma continues it when it names a clock, a dimension or an
    entity ("by incident name, incident" lists two). A window the question states is not part
    of a grouping's name: it separates pieces as a comma does, so the list goes on past it only
    with a piece that names one ("by store, last month" lists one; "by incident name, last
    month and incident" lists two). A listed grouping the draft lacks holds the plan, so
    reading more of the question can hold more plans but never makes one ready.
    """

    lowered = str(text or "").lower()
    match = re.search(
        r"^\s*(?:the\s+)?top\s+([a-z0-9 _,-]+?)\s+by\s+[a-z0-9 _-]+?(?:[.?!,;]|$)", lowered
    )
    if match:
        raw_terms = _strip_leading_rank_count(match.group(1).strip())
    else:
        match = re.search(
            r"\bby ([a-z0-9 _,-]+?)(?:\s+(?:where|for|from|in|with|during|over|having|who|that)\b|[.?!;]|$)",
            lowered,
        )
        raw_terms = match.group(1).strip() if match else ""
    if not match or not raw_terms:
        return []
    # A recorded window is a clause boundary, read as a comma.
    offset = match.start(1) + match.group(1).find(raw_terms)
    for start, end in _time_window(text).spans:
        low, high = max(start, offset) - offset, min(end, offset + len(raw_terms)) - offset
        if low < high:
            raw_terms = raw_terms[:low] + "," + " " * (high - low - 1) + raw_terms[high:]
    pieces = re.split(r"(\s*(?:,\s*and |,| and | & | by )\s*)", raw_terms)
    terms: list[str] = []
    for index in range(0, len(pieces), 2):
        term = pieces[index].strip()
        if not term:
            continue
        if (
            index
            and "," in pieces[index - 1]
            and not (
                _is_temporal_grouping_term(term)
                or any(_names_time_axis(term, row.label) for row in config.temporal_roles)
                or any(
                    row.calendar_id and _names_time_axis(term, row.label) for row in config.entities
                )
                or any(_grouping_matches(term, row) for row in config.dimensions)
                or any(_grouping_matches(term, row, entity=True) for row in config.entities)
            )
        ):
            break
        terms.append(term)
    return terms


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
    for domain in visible_value_domains(config):
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
        dim = _object_by_id(visible_dimensions(config), "dimension.jaffle_customer_history_segment")
        if dim is not None:
            group_by.append(dim.id)
    if any(term in lowered for term in ("geo", "geography", "region", "parent")):
        dim = _dimension(config, ["geo"], prefer_parent="parent" in lowered)
        if dim is not None:
            group_by.append(dim.id)
    for term in _requested_grouping_terms(text):
        term_tokens = set(_tokens(term))
        if (
            "geo" in term_tokens
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
