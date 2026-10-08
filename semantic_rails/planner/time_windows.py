"""Resolve a question's time window, as-of cues and fiscal calendar."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from ..ast import _parse_now, _relative_range_bounds
from ..errors import SemanticLayerError
from ._base import _tokens
from .filter_checks import _excluded_value_spans
from .time_phrases import (
    _BOUNDARY_BEFORE_RE,
    _COMPARISON_GUARD,
    _DAY_RANGE_RE,
    _FISCAL_RE,
    _ISO_RANGE_RE,
    _MONTH_RANGE_SHARED_YEAR_RE,
    _MONTH_YEAR_RANGE_RE,
    _NUMBER_WORD_ALT,
    _TIME_UNIT_ALT,
    _YEAR_SPAN_RE,
    _YEAR_TOKEN_RE,
    _all_time_spans,
    _AsOfCue,
    _calendar_windows,
    _named_calendar_windows,
    _overlaps,
    _relative_window,
    _time_cues,
)
from .time_reference import time_policy_context, time_timezone
from .visibility import visible_dimensions


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
        row.id
        for row in visible_dimensions(config)
        if row.entity == calendar.id and row.column == column
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


# "revenue in 2017 over 2016", "more revenue in 2017 than in 2016": a
# comparison of two years, even where "over 2000" alone would be a quantity.
_YEAR_COMPARISON_RE = re.compile(
    r"\b20\d{2}\s+(?:over|above|below|under|than|exceeding|versus|vs\.?|against|"
    r"compared\s+(?:to|with)|relative\s+to)\s+(?:in\s+|the\s+)?(?:year\s+)?20\d{2}\b"
)


# Range forms whose spoken end is inclusive, with the unit that end names. The Query IR
# end is exclusive, so plan states the reading it took.
_RANGE_END_UNITS: dict[re.Pattern[str], str] = {
    _ISO_RANGE_RE: "day",
    _DAY_RANGE_RE: "day",
    _MONTH_YEAR_RANGE_RE: "month",
    _MONTH_RANGE_SHARED_YEAR_RE: "month",
    _YEAR_SPAN_RE: "year",
}

# plan resolves days and coarser windows only. A window shorter than a day ("last 24 hours",
# "past hour") is reported and the window is left unset, never widened to all time. Hours and
# zones stated any other way ("9 am", "between 9 and 17", "UTC") are caught where plan decides
# readiness: every numeral and clock word must be accounted for (see unconsumed_terms).
_SUBDAY_WINDOW_RE = re.compile(
    rf"{_COMPARISON_GUARD}\b(?:(?:last|past|previous|prior|trailing)"
    rf"|(?:current|this))\s+(?:(?:\d+|{_NUMBER_WORD_ALT}|an?|few|couple(?:\s+of)?|several)\s+)?"
    r"(?:second|minute|hour)s?\b"
)


@dataclass(frozen=True)
class _TimeWindow:
    """What a question says about time: its window, or what couldn't be resolved."""

    bounds: dict[str, Any] = field(default_factory=dict)
    relative_unit: str = ""
    unresolved: tuple[str, ...] = ()
    # Every span of the lowercased text read as time, resolved or not.
    spans: tuple[tuple[int, int], ...] = ()
    # Windows the question states that differ from one another.
    conflicts: tuple[str, ...] = ()
    # The reading taken where the question leaves an end open.
    assumptions: tuple[str, ...] = ()
    # The windows shorter than a day named, which plan does not resolve.
    sub_day: tuple[str, ...] = ()
    # Every window the question states that plan resolved, as (span, bounds), whether or not
    # another phrase left the question unresolved.
    windows: tuple[tuple[tuple[int, int], dict[str, Any]], ...] = ()
    as_of: tuple[_AsOfCue, ...] = ()
    # Explicit alternatives for a named current period or a mismatched weekday.
    readings: tuple[str, ...] = ()


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


# What may sit between a window and its restatement: "(", ",", ":", "i.e.".
_RESTATEMENT_JOIN_RE = re.compile(r"^\s*[(,:;–—-]?\s*(?:(?:i\.?e\.?|that\s+is)\s*,?\s*)?$")


def _is_restatement(
    lowered: str, windows: list[tuple[tuple[int, int], dict[str, Any], str]]
) -> bool:
    """Whether every window says the same thing about the one before it.

    Equal bounds are not enough: "revenue in 2017 from customers who signed up in 2017" has
    two windows with one set of bounds and two different conditions. A restatement sits
    right beside what it restates, joined by a bracket, a comma or "i.e.".
    """

    return all(
        row[1] == windows[0][1]
        and _RESTATEMENT_JOIN_RE.match(lowered[before[0][1] : row[0][0]]) is not None
        for before, row in zip(windows, windows[1:], strict=False)
    )


def _time_window(
    text: str,
    policy_context: dict[str, Any] | None = None,
    *,
    timezone: str | None = None,
) -> _TimeWindow:
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
    context = time_policy_context(policy_context)
    now = (
        _parse_now(context)
        if context and context.get("now") not in (None, "")
        else datetime.now(UTC)
    )
    if isinstance(now, datetime):
        if now.tzinfo is not None:
            now = now.astimezone(ZoneInfo(timezone or time_timezone()))
        now = now.date()
    today = now
    return copy.deepcopy(_resolved_time_window(lowered, today))


# Longer questions are left unresolved; the resolver's cost grows with the
# square of the text. Never resolve only a prefix.
_MAX_TIME_TEXT = 2000


_AS_OF_NOW_RE = re.compile(
    rf"\b(?:(?:as\s+of\s+|right\s+)?now|currently|at\s+the\s+moment|"
    rf"current(?!\s+(?:{_TIME_UNIT_ALT}|hour|minute|second)s?\b))\b"
)
_AS_OF_WINDOW_RE = re.compile(
    r"\b(?:(?:as\s+of\s+)?(?:at\s+the\s+|the\s+)?end\s+of|as\s+of)\s+(?:the\s+)?"
)


def _as_of_cues(lowered: str, today: date) -> tuple[_AsOfCue, ...]:
    """Read complete as-of phrases before interval parsing can claim their suffix."""

    cues = [
        _AsOfCue("latest_complete_day", match.span()) for match in _AS_OF_NOW_RE.finditer(lowered)
    ]
    for lead in _AS_OF_WINDOW_RE.finditer(lowered):
        if _overlaps(lead.span(), [cue.span for cue in cues]):
            continue
        suffix = lowered[lead.end() :]
        accepted, rejected = _calendar_windows(suffix)
        candidates = [(span, bounds) for span, bounds, _form in accepted]
        candidates += [(span, bounds) for span, bounds, _unit in _relative_window(suffix, today)]
        candidates += [(span, {}) for span in rejected + _time_cues(suffix)]
        candidates = [row for row in candidates if row[0][0] == 0]
        if not candidates:
            # Keep an unread as-of lead held as well; never admit its suffix as an interval.
            cues.append(_AsOfCue("closing_day", lead.span()))
            continue
        span, bounds = max(candidates, key=lambda row: row[0][1])
        if bounds.get("range"):
            try:
                bounds = _relative_range_bounds(bounds["range"], policy_context={"now": today})
            except (SemanticLayerError, ValueError, OverflowError):
                bounds = {}
        cues.append(_AsOfCue("closing_day", (lead.start(), lead.end() + span[1]), bounds))
    return tuple(sorted(cues, key=lambda cue: cue.span))


@lru_cache(maxsize=512)
def _resolved_time_window(lowered: str, today: date) -> _TimeWindow:
    as_of = _as_of_cues(lowered, today)
    # Mask exactly the recorded spans: no parser may resolve part of an as-of phrase.
    interval_text = lowered
    for cue in reversed(as_of):
        start, end = cue.span
        interval_text = interval_text[:start] + " " * (end - start) + interval_text[end:]
    accepted, rejected = _calendar_windows(interval_text, boundary_text=lowered, as_of=as_of)
    relative = _relative_window(interval_text, today)
    if _FISCAL_RE.search(lowered):
        # A fiscal question's "last quarter" or "this year" is a fiscal period.
        rejected += [row[0] for row in relative if row[2] != "day"]
        relative = [row for row in relative if row[2] == "day"]
    windows: list[tuple[tuple[int, int], dict[str, Any], str]] = [
        (span, bounds, "") for span, bounds, _pattern in accepted
    ]
    for row in relative:
        if not _overlaps(row[0], [item[0] for item in windows]):
            windows.append(row)
    named, named_rejected, assumptions, readings = _named_calendar_windows(
        interval_text, today, [row[0] for row in windows] + rejected + [cue.span for cue in as_of]
    )
    windows.extend(named)
    rejected.extend(named_rejected)
    all_time = _all_time_spans(interval_text)
    windows.extend((span, {}, "") for span in all_time)
    # Every parser passes through the same exclusion grammar: a negative clause
    # cannot become a positive date window, even with unrelated negative filters.
    excluded = _excluded_value_spans(lowered)
    rejected.extend(
        span
        for span, _bounds, _unit in windows
        if any(start <= span[0] and span[1] <= end for start, end in excluded)
    )
    windows = [row for row in windows if row[0] not in rejected]
    # "Ever" and "in total" emphasize one bounded window; other all-time forms
    # still conflict with it. Record the intensifiers' exact spans for consumption.
    intensifiers = []
    if sum(bool(row[1]) for row in windows) == 1:
        intensifiers = [
            span
            for span, bounds, _unit in windows
            if not bounds and " ".join(lowered[span[0] : span[1]].split()) in {"ever", "in total"}
        ]
        windows = [row for row in windows if row[0] not in intensifiers]
    if any(not row[1] for row in windows):
        assumptions.append("all time: no start date")
    windows.sort(key=lambda row: row[0])
    covered = [row[0] for row in windows]
    unread = [cue.span for cue in as_of] + [
        span for span in rejected if not _overlaps(span, covered)
    ]
    # A range ending in an as-of cue is one unread phrase, including its connector.
    unresolved_spans: list[tuple[int, int]] = []
    for start, end in sorted(unread):
        if unresolved_spans and start < unresolved_spans[-1][1]:
            prior_start, prior_end = unresolved_spans.pop()
            unresolved_spans.append((prior_start, max(prior_end, end)))
        else:
            unresolved_spans.append((start, end))
    if len(windows) > 1 and _is_restatement(lowered, windows):
        span = (windows[0][0][0], windows[-1][0][1])
        windows = [(span, windows[0][1], next((row[2] for row in windows if row[2]), ""))]
    # Two years compared ("2017 over 2016") are reported, whatever resolved.
    unresolved_spans += [match.span() for match in _YEAR_COMPARISON_RE.finditer(lowered)]
    # Longest cues first, so a year inside "4/3/2017" isn't reported twice.
    for span in sorted(_time_cues(lowered), key=lambda item: item[0] - item[1]):
        if not _overlaps(span, covered + intensifiers + unresolved_spans):
            unresolved_spans.append(span)
    # A window shorter than a day is reported, never dropped to all time.
    sub_day = [
        match.span()
        for match in _SUBDAY_WINDOW_RE.finditer(lowered)
        if not _overlaps(match.span(), covered)
    ]
    unresolved_spans += [span for span in sub_day if not _overlaps(span, unresolved_spans)]
    time_spans = tuple(sorted(covered + intensifiers + unresolved_spans))
    if unresolved_spans or len(windows) > 1:
        # Report every time phrase, resolved or not: resolving part of an
        # ambiguous question would answer a different one.
        spans = sorted(unresolved_spans + (covered if len(windows) > 1 else []))
        phrases = list(dict.fromkeys(_phrase(lowered, span) for span in spans))
        conflicts = (
            tuple(dict.fromkeys(_phrase(lowered, span) for span in sorted(covered)))
            if len(windows) > 1
            else ()
        )
        return _TimeWindow(
            unresolved=tuple(phrases),
            spans=time_spans,
            conflicts=conflicts,
            sub_day=tuple(dict.fromkeys(_phrase(lowered, span) for span in sorted(sub_day))),
            windows=tuple((row[0], dict(row[1])) for row in windows),
            as_of=as_of,
            readings=tuple(readings),
        )
    if not windows:
        return _TimeWindow()
    _span, bounds, unit = windows[0]
    for span, _bounds, pattern in accepted:
        end_unit = _RANGE_END_UNITS.get(pattern)
        if end_unit and bounds == _bounds:
            assumptions.append(
                f"'{lowered[span[0] : span[1]].strip()}' includes its last {end_unit}, so "
                f"time.end is {bounds['end']} (exclusive)."
            )
    return _TimeWindow(
        bounds=dict(bounds),
        relative_unit=unit,
        spans=time_spans,
        assumptions=tuple(dict.fromkeys(assumptions)),
        windows=((_span, dict(bounds)),),
    )


def _time_bounds_from_text(text: str) -> dict[str, Any]:
    return dict(_time_window(text).bounds)
