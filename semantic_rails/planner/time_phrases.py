"""Time phrases plan reads: calendar and relative-window grammar, bounds, cues and comparison triggers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ._base import _NUMBER_WORDS, _tokens


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
# window ("in 2017", "for 2017", "during 2017") or after the word "year"
# ("year 2017", "the calendar year 2017"). Anything else ("2017 revenue",
# "early 2017", "end of 2017", "2000 customers") is reported, never guessed.
_YEAR_IN_RE = re.compile(
    rf"\b(?:(?:in|for|during|throughout|within)\s+(?:the\s+)?(?:(?:calendar\s+)?year\s+)?"
    rf"|(?:the\s+)?(?:calendar\s+)?year\s+){_YEAR}\b"
)
# "year 2017" with no scoping preposition stands for a calendar year only where nothing
# qualifies it: at the start of the text or after punctuation. After a word it is a
# different year ("financial year 2017", "model year 2017", "tax year 2017"), reported.
_BARE_YEAR_LEAD_RE = re.compile(r"(?:the\s+)?(?:calendar\s+)?year\b")
_UNQUALIFIED_BEFORE_RE = re.compile(r"(?:^|[,;:(])\s*$")
_LAST_WORD_RE = re.compile(r"[^\s,;:()]+\s*$")

# A calendar phrase directly after one of these is a bound or a comparison,
# not a window ("before 2017", "since March 2017", "as of June 30, 2017",
# "2017 vs 2016"), and a phrase after "of" is qualified ("the end of
# 2017", "the week of April 3, 2017"). The planner reports these.
_BOUNDARY_BEFORE_RE = re.compile(
    r"(?:\b(?:before|after|since|until|till|through|thru|by|from|ending|starting|beginning|"
    r"prior\s+to|up\s+to|as\s+of|earlier\s+than|later\s+than|pre|post|vs\.?|versus|"
    r"compared\s+(?:to|with)|relative\s+to|against|over|than|of|"
    r"last|prior|previous|past|next|this|current|each|every|per)[\s-]+(?:the\s+)?)$"
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


# Range forms whose connector may be "and": "and" makes a range only after
# "between" ("between March and May 2017"); "March and May 2017" names two
# periods, which the planner reports rather than spanning.
_AND_JOINABLE_FORMS: tuple[re.Pattern[str], ...] = (
    _ISO_RANGE_RE,
    _DAY_RANGE_RE,
    _MONTH_YEAR_RANGE_RE,
    _MONTH_RANGE_SHARED_YEAR_RE,
)

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
class _AsOfCue:
    """A snapshot request, kept separate from an ordinary interval."""

    kind: str
    span: tuple[int, int]
    bounds: dict[str, Any] = field(default_factory=dict)


def _overlaps(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in spans)


def _calendar_windows(
    lowered: str,
    *,
    boundary_text: str | None = None,
    as_of: tuple[_AsOfCue, ...] = (),
) -> tuple[list[tuple[tuple[int, int], dict[str, str], re.Pattern[str]]], list[tuple[int, int]]]:
    """Resolved calendar spans with their bounds and form, and calendar spans rejected as bounds."""

    accepted: list[tuple[tuple[int, int], dict[str, str], re.Pattern[str]]] = []
    rejected: list[tuple[int, int]] = []
    fiscal = _FISCAL_RE.search(lowered) is not None
    for pattern, to_bounds in _CALENDAR_FORMS:
        for match in pattern.finditer(lowered):
            span = match.span()
            if _overlaps(span, [row[0] for row in accepted] + rejected):
                continue
            original = boundary_text if boundary_text is not None else lowered
            before, after = original[: span[0]], original[span[1] :]
            adjacent_cue = False
            for cue in as_of:
                if cue.span[0] >= span[1] and re.fullmatch(
                    _RANGE_CONNECTOR, original[span[1] : cue.span[0]].strip()
                ):
                    span = (span[0], cue.span[1])
                    adjacent_cue = True
                elif cue.span[1] <= span[0] and re.fullmatch(
                    _RANGE_CONNECTOR, original[cue.span[1] : span[0]].strip()
                ):
                    span = (cue.span[0], span[1])
                    adjacent_cue = True
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
            qualified = bool(
                pattern is _YEAR_IN_RE
                and _BARE_YEAR_LEAD_RE.match(text)
                and not _UNQUALIFIED_BEFORE_RE.search(before)
            )
            if qualified and not boundary:
                # Report the qualifier with its year: "financial year 2017".
                word = _LAST_WORD_RE.search(before)
                span = (word.start() if word else span[0], span[1])
            range_after = _UNPARSED_RANGE_AFTER_RE.search(after)
            if (
                not bounds
                or boundary
                or qualified
                or (fiscal and pattern not in _DAY_EXACT_FORMS)
                or _UNPARSED_RANGE_BEFORE_RE.search(before)
                or range_after
                or adjacent_cue
            ):
                rejected.append(span)
                continue
            accepted.append((span, bounds, pattern))
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
