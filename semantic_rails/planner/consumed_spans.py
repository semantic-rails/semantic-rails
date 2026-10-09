"""Numerals, clock and zone words the draft must consume."""

from __future__ import annotations

import re
from typing import Any

from ._base import _TERM_SYNONYMS, _name_matches, _singular, _tokens
from .coverage import (
    _catalog_rows,
    _dict_nodes,
    _is_number,
    _plain,
    _referenced_ids,
    _time_block,
    _value_names,
    _value_phrases,
)
from .ranking_checks import _dimension_nouns, _ranking_at, _ranking_words
from .snapshot import snapshot_read
from .time_checks import _question_time, _window_agrees


def _ranking_count_spans(text: str, limit: int, nouns: frozenset[str]) -> list[tuple[int, int]]:
    """Where the question states ``limit`` as the count of a ranking ("top 5 stores", "the 5
    customers who spent the most", "3 stores with the least revenue", "which 2 stores ...")."""

    scanned = _ranking_words(text)
    words = [word for word, _start, _end in scanned]
    spans: list[tuple[int, int]] = []
    for index in range(len(words)):
        request = _ranking_at(words, index, nouns)
        if request is not None and request["limit"] == limit and request["count_at"] is not None:
            _word, start, end = scanned[request["count_at"]]
            spans.append((start, end))
    return spans


# Words that state a clock time or a zone. Like a numeral, one no part of the draft consumes
# means the answer may differ from the question (see unconsumed_terms). "second" alone is an
# ordinal ("second order"); a count of seconds is caught as a numeral. "min", "hr" and "am"
# are left out: they only carry meaning beside a numeral, and the numeral is caught.
_CLOCK_WORDS = frozenset(
    {"seconds", "minute", "minutes", "hour", "hours", "hourly", "clock"}
    | {"noon", "midnight", "midday", "morning", "afternoon", "evening", "overnight", "tonight"}
    | {"utc", "gmt", "zulu", "tz", "timezone", "timezones"}
)
# Spelled-out numbers: "from nine to five", "at twelve", "half past two".
_SPOKEN_NUMBERS: dict[str, float] = {
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "six": 6.0,
    "seven": 7.0,
    "eight": 8.0,
    "nine": 9.0,
    "ten": 10.0,
    "eleven": 11.0,
    "twelve": 12.0,
    "thirteen": 13.0,
    "fourteen": 14.0,
    "fifteen": 15.0,
    "sixteen": 16.0,
    "seventeen": 17.0,
    "eighteen": 18.0,
    "nineteen": 19.0,
    "twenty": 20.0,
    "thirty": 30.0,
    "forty": 40.0,
    "fifty": 50.0,
    "sixty": 60.0,
    "seventy": 70.0,
    "eighty": 80.0,
    "ninety": 90.0,
    "hundred": 100.0,
    "half": 0.5,
}
# "quarter" is a period ("last quarter"); only "quarter past" and "quarter to" tell a time.
_QUARTER_CLOCK_RE = re.compile(r"\bquarter\s+(?:past|to|after|till|until|before)\b")
# The zone codes a question can carry; docs/MCP_INTERFACE.md lists them. Codes that are also
# words ("cat", "eat", "west") never count by their lowercase spelling.
_ZONE_CODES = frozenset(
    {"est", "edt", "cst", "cdt", "mst", "mdt", "pst", "pdt", "akst", "akdt", "hst", "cet", "cest"}
    | {"eet", "eest", "bst", "ist", "jst", "kst", "msk", "aest", "aedt", "acst", "acdt", "awst"}
    | {"nzst", "nzdt", "sgt", "hkt", "wib", "sast", "pkt"}
)
# Short codes count only in capitals, in a question that isn't all capitals ("in ET",
# "12:00 PT", "12:00 Z"; "Z" is Zulu). Words that are also zone codes ("west", "cat") never do.
_CASED_ZONE_CODES = frozenset({"Z", "ET", "PT", "CT", "MT"})
_ZONE_NAME_RE = re.compile(
    r"\b(?:africa|america|antarctica|arctic|asia|atlantic|australia|europe|indian|pacific|etc)"
    r"/[a-z_]+",
    re.IGNORECASE,
)
_TERM_RE = re.compile(r"\d+(?:[.,]\d+)+(?![^\W_])|[^\W_]+")


def _number_key(value: float) -> str | None:
    """A number as digits, or ``None`` for one too large to read (a caller's 10**400)."""

    try:
        number = float(value)
        return str(int(number)) if number.is_integer() else repr(number)
    except (OverflowError, ValueError):
        return None


def unconsumed_terms(runtime: Any, question: str, query: dict[str, Any]) -> list[str]:
    """The numerals, number words and clock or zone words no part of the draft consumes.

    The invariant plan holds a draft to before it calls it ready: every number ("9", "14h30",
    "nine"), clock word ("hour", "noon", "o'clock") and zone ("UTC", "EST", "Z",
    "Europe/Berlin") in the question sits inside the character span of a construct the draft
    carries: a date or window, the limit, threshold or percentile it states, a filter value, or
    the name of an object it selects. Consumption is by span, never by value alone: a "1930"
    that no window phrase or threshold's own number holds is left over, and so is any number
    no construct reads. One left over is an hour, a range or a threshold the draft silently
    dropped.
    """

    text = str(question or "")
    lowered = text.lower()
    tokens = [(match.group(0), *match.span()) for match in _TERM_RE.finditer(lowered)]
    terms = _time_and_number_terms(text, lowered, tokens)
    # "Europe/Berlin" reads as two plain words, so it is one term, named whole.
    terms.extend((zone.group(0), *zone.span()) for zone in _ZONE_NAME_RE.finditer(lowered))
    if not terms:
        return []
    consumed = _consumed_spans(runtime, lowered, tokens, query)
    out: list[str] = []
    for word, start, end in sorted(terms, key=lambda term: term[1]):
        inside = any(low <= start and end <= high for low, high in consumed)
        if not inside and word not in out:
            out.append(word)
    return out


def _time_and_number_terms(
    text: str, lowered: str, tokens: list[tuple[str, int, int]]
) -> list[tuple[str, int, int]]:
    """Every numeral, number word, clock word and zone code of the question, with its span."""

    # Short codes are read in the question's own case; the lengths must agree to slice by span.
    cased = len(lowered) == len(text)
    mixed = not cased or text != text.upper()
    quarters = {match.start() for match in _QUARTER_CLOCK_RE.finditer(lowered)}
    out: list[tuple[str, int, int]] = []
    for word, start, end in tokens:
        original = text[start:end] if cased else word.upper()
        if (
            any(char.isdigit() for char in word)
            or word in _SPOKEN_NUMBERS
            or word in _CLOCK_WORDS
            or word in _ZONE_CODES
            or start in quarters
            or (mixed and original in _CASED_ZONE_CODES)
        ):
            out.append((word, start, end))
    return out


def _consumed_spans(
    runtime: Any, lowered: str, tokens: list[tuple[str, int, int]], query: dict[str, Any]
) -> list[tuple[int, int]]:
    """The character spans of the question the draft's constructs consume.

    A window consumes the date phrases the planner resolved when the draft carries one (both
    bounds, or a range) that agrees with them (``_window_agrees``), and the phrases it could
    not resolve; never a clock time or a bare year. A limit
    consumes the count of the ranking that states it ("top 5", "the 5 customers who spent the
    most"); a threshold, percentile or numeric filter value consumes its own number token, found
    where the question states it ("over 12.50", "90th percentile", "1,000 or more", "size 12").
    A filter value, or the name of an object the draft selects, consumes the tokens that spell it.
    """

    spans: list[tuple[int, int]] = []
    time = _time_block(query)
    spans.extend(_window_spans(lowered, time, query.get("policy_context")))
    if (read := snapshot_read(runtime, lowered, query)) is not None:
        # A balance read on the day an as-of phrase or a window names consumes that phrase.
        spans.extend(read.spans)
    normal = [_singular(_TERM_SYNONYMS.get(word, word)) for word, _start, _end in tokens]
    referenced = set(_referenced_ids(query))
    calendar_id = str(time.get("calendar_id") or "default")
    rows = _catalog_rows(runtime._config)
    for row in rows:
        if str(getattr(row, "id", "")) in referenced or (
            calendar_id != "default" and getattr(row, "calendar_id", "") == calendar_id
        ):
            # An object's own names only: a description that says "per hour" consumes nothing.
            names = [str(getattr(row, attr, "") or "") for attr in ("id", "name", "label")]
            spans.extend(_name_spans(tokens, normal, names, whole=False))
            spans.extend((start, end) for _, start, end in _name_matches(row, lowered))
    labels = _value_phrases(runtime._config)
    fields = {str(getattr(row, "id", "")): row for row in rows}
    for node in _dict_nodes(query):
        if "field" not in node or "value" not in node:
            continue
        value = node.get("value")
        items = value if isinstance(value, list) else [value]
        names = [str(item) for item in items if not _is_number(item)]
        for item in list(names):
            # A filter on 'jaffle' consumes the question's "food".
            for _domain, row in labels.get(_plain(item), []):
                names.extend(_value_names(row))
        spans.extend(_name_spans(tokens, normal, names, whole=True))
        row = fields.get(str(node["field"]))
        if row is not None:
            # A number filter consumes the number token beside its field's name ("size 12").
            field_words = {
                word
                for attr in ("id", "name", "label")
                for word in _normal_tokens(str(getattr(row, attr, "") or ""))
            }
            spans.extend(
                _number_spans(
                    lowered, tokens, *_draft_numbers({"value": items}), normal, field_words
                )
            )
    spans.extend(_number_spans(lowered, tokens, *_draft_numbers(query)))
    limit = query.get("limit")
    limit = _number_key(limit) if isinstance(limit, (int, float)) else None
    if limit is not None and limit.isdigit():
        # A ranking's count is read where the ranking parser reads it, whatever words sit beside
        # it. A limit the draft carries and the question doesn't state stays unconsumed.
        nouns = _dimension_nouns(runtime._config)
        spans.extend(_ranking_count_spans(lowered, int(limit), nouns))
    return spans


def _normal_tokens(text: str) -> tuple[str, ...]:
    return tuple(_singular(token) for token in _tokens(text))


def _name_spans(
    tokens: list[tuple[str, int, int]], normal: list[str], names: list[str], whole: bool
) -> list[tuple[int, int]]:
    """Where the question spells a name: all of it (a filter value), or any run of its words (an
    object's name: "by hour" for "Hour of day")."""

    spans: list[tuple[int, int]] = []
    for name in names:
        phrase = _normal_tokens(name)
        if not phrase:
            continue
        for first in range(len(normal)):
            for offset in range(len(phrase)):
                if whole and offset:
                    break
                length = 0
                while (
                    first + length < len(normal)
                    and offset + length < len(phrase)
                    and normal[first + length] == phrase[offset + length]
                ):
                    length += 1
                if length and (not whole or length == len(phrase)):
                    spans.append((tokens[first][1], tokens[first + length - 1][2]))
    return spans


# Where a question states a threshold or a percentile: a comparison before its number ("over
# 12.50", "at least 2") or after it ("1,000 or more", "5+"). Never a ranking's cue ("top",
# "first", "10 largest"): a ranking's count is the limit's, read by the ranking parser, so a
# threshold cannot consume it and the limit cannot consume a threshold.
_NUMBER_BEFORE_RE = re.compile(
    r"(?:\b(?:more\s+than|greater\s+than|over|above|exceeds?|exceeded|exceeding|at\s+least"
    r"|no\s+fewer\s+than|minimum\s+of|less\s+than|fewer\s+than|under|below|at\s+most"
    r"|no\s+more\s+than|maximum\s+of|equals?|equal\s+to)|[<>=]=?)[\s-]*[$£€]?$"
)
_NUMBER_AFTER_RE = re.compile(
    r"^\s*(?:\+|or\s+(?:more|less|fewer|greater|higher|lower|above|below|over|under)\b)"
)
# A number is a percentage only when the question says so: "50 %", "50 percent", "90th percentile".
_PERCENT_AFTER_RE = re.compile(r"^\s*(?:%|percent\b|pct\b|(?:st|nd|rd|th)?\s*percentile\b)")


def _token_value(word: str) -> float | None:
    """The number a token states: "12.50" is 12.5, "90th" is 90, "five" is 5."""

    plain = re.fullmatch(r"(\d+(?:[.,]\d+)*)(?:st|nd|rd|th)?", word)
    try:
        return float(plain.group(1).replace(",", "")) if plain else _SPOKEN_NUMBERS.get(word)
    except ValueError:
        return None


def _number_spans(
    lowered: str,
    tokens: list[tuple[str, int, int]],
    numbers: set[str],
    percents: set[str],
    normal: list[str] | None = None,
    near: set[str] | None = None,
) -> list[tuple[int, int]]:
    """The number tokens that state one of the draft's numbers where a construct states it: a
    threshold or percentile cue beside it, or (with ``near``) a filter's field name.

    A token is a percentage only with a percent cue after it: "50 percent" states 0.5 (and
    "10 percent" the 0.9 of a "top 10 percent" cut), while a bare "50" or "500" never does.
    """

    spans: list[tuple[int, int]] = []
    for index, (word, start, end) in enumerate(tokens):
        value = _token_value(word)
        if value is None:
            continue
        percent = _PERCENT_AFTER_RE.match(lowered[end : end + 60]) is not None
        key = _number_key(value)
        if not (
            key in numbers or (percent and (key in percents or _number_key(value / 100) in numbers))
        ):
            continue
        if near is not None and normal is not None:
            stated = bool(near & set(normal[max(index - 1, 0) : index + 2]))
        else:
            stated = (
                percent
                or _NUMBER_BEFORE_RE.search(lowered[max(start - 40, 0) : start]) is not None
                or _NUMBER_AFTER_RE.match(lowered[end : end + 60]) is not None
            )
        if stated:
            spans.append((start, end))
    return spans


def _window_spans(
    lowered: str,
    time: dict[str, Any],
    policy_context: dict[str, Any] | None = None,
    *,
    timezone: str | None = None,
) -> list[tuple[int, int]]:
    """The spans of the question a window in the draft's ``query.time`` consumes.

    One rule: the draft's window consumes the date phrases plan resolved only if it agrees with
    them (``_window_agrees``); otherwise it consumes none. It also answers a phrase plan cannot
    resolve ("last 24 hours"). It never consumes a bare year, a time of day, an hour, a bare
    number or a zone, whatever the window's bounds say, so a question that states one is
    refused rather than matched to the window by value.
    """

    windows, others = _question_time(lowered, policy_context, timezone=timezone)
    if not windows and not any(time.get(key) for key in ("start", "end", "range")):
        return []
    if not _window_agrees(windows, time, policy_context, timezone=timezone):
        return []
    return [span for span, _bounds in windows] + others


def _draft_numbers(query: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Every number the draft carries outside its time block and its limit (a threshold, a
    filter value, a percentile), as digits, and the percentages its fractions state (0.9 is the
    "top 10 percent" it cuts). The limit is read by the ranking that states it, never here: a
    threshold that repeats its number ("top 10 stores with at least 10 orders") is not the limit."""

    return _number_sets(
        {key: value for key, value in query.items() if key not in ("time", "limit")}
    )


def _number_sets(query: dict[str, Any]) -> tuple[set[str], set[str]]:
    """The numbers a query carries as digits, and the percentages its fractions state: 0.5
    states "50" (and the "50" of a "top 50 percent" cut)."""

    plain: set[str] = set()
    percents: set[str] = set()

    def add(value: float) -> None:
        key = _number_key(value)
        if key is None:
            return
        plain.add(key)
        if 0 < value < 1 and float(value * 100).is_integer():
            percents.update({str(int(value * 100)), str(100 - int(value * 100))})

    def walk(value: Any) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            add(value)
        elif isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)?", value.strip()):
            add(float(value))
        elif isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk({key: value for key, value in query.items() if key != "version"})
    return plain, percents


__all__ = [
    "unconsumed_terms",
]
