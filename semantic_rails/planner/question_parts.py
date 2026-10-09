"""Split a question that asks several things into parts, each planned on its own words.

"Last week, how many accounts signed up, and what was the MRR?" asks two things that may need
two clocks, which one Query IR can't carry. Plan splits such a question only at a top-level
clause boundary: ", and", "," or "and" immediately before a wh-word or "how many" / "how much".
A leading phrase that asks nothing ("Last week, ...") applies to every part; nothing else is
shared. A boundary inside quotes, parentheses or a grouping list ("by plan and region") is not
one. The split only reads where clauses start: each part is then planned like any question,
and the whole is ready only when every part is (``plan._parts_payload``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .groupings import _requested_grouping_spans
from .time_phrases import _TIME_UNITS
from .time_windows import _time_window
from .unasked_groupings import _asked_grouping_terms, _asks_grain
from .unmatched_words import _FRAMING_WORDS

# Quoted text, which is never split and keeps its spelling.
QUOTED = r"\"[^\"]*\"|“[^”]*”|(?<!\w)['‘].*?['’](?!\w)"
_OPENER = r"(?:what|which|who|whom|whose|when|where|why|how\s+(?:many|much))\b"
_BOUNDARY_RE = re.compile(
    rf"\s*,\s*and\s+(?={_OPENER})|\s*,\s*(?={_OPENER})|\s+and\s+(?={_OPENER})", re.IGNORECASE
)
_OPENS_RE = re.compile(rf"\s*{_OPENER}", re.IGNORECASE)
_PROTECTED_RE = re.compile(rf"{QUOTED}|\([^()]*\)")
_WORD_RE = re.compile(r"[^\W_]+")
# Words that point at what another part asks for ("and what share of those closed?"), and
# phrases that do ("and how much of that came from new accounts?"); a bare "that" doesn't.
_BACK_REFERENCES = frozenset({"those", "these", "them", "they", "their", "theirs", "it", "its"})
_BACK_REFERENCE_PHRASES = frozenset({("of", "that"), ("of", "this"), ("of", "which")})
MAX_PARTS = 4


@dataclass(frozen=True)
class QuestionPart:
    """One part's own words, after the shared leading phrase, and its spans in the question."""

    text: str
    spans: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class QuestionSplit:
    parts: tuple[QuestionPart, ...]
    # Why plan can't plan the parts on their own: a hold code and the parts it names (1-based).
    hold: str = ""
    held: tuple[int, ...] = ()


def _asks_grouping(config: Any, text: str) -> bool:
    """Whether the text asks for a grouping as plan reads one: a grouping it lists, the noun
    it ranks or the words after "per", "each" or "every" (``_asked_grouping_terms``), or a time
    unit's buckets or a series ("monthly", "over time", ``_asks_grain``)."""

    return bool(
        _requested_grouping_spans(text.lower())
        or _asked_grouping_terms(config, text)
        or _asks_grain(text, _TIME_UNITS)
    )


def split_question(question: str, config: Any) -> QuestionSplit | None:
    """The question's parts, or None when it asks one thing.

    The text before the first boundary is a shared leading phrase when it asks nothing (no
    wh-word or "how many") and a bare comma ends it. A split is held when it has more than
    ``MAX_PARTS`` parts, when a part after the first points back at another ("those", "of
    that"), when a part names nothing to measure ("and how many?"), or when some parts state a
    time window or a grouping (``_asks_grouping``: "by plan", "per plan", "monthly") and others
    don't: a trailing "last week" or "per plan" may be meant for every part, and plan never
    guesses which.
    """

    from ..metadata_parts.relevance import _INTENT_STOPWORDS  # noqa: WPS433

    lowered = question.lower()
    protected = [match.span() for match in _PROTECTED_RE.finditer(question)]
    if groupings := _requested_grouping_spans(lowered):
        protected.append((groupings[0][0], groupings[-1][1]))
    cuts = [
        match
        for match in _BOUNDARY_RE.finditer(question)
        if not any(low <= match.start() < high for low, high in protected)
    ]
    if not cuts:
        return None
    bounds = [0, *(at for cut in cuts for at in cut.span()), len(question)]
    segments = [(bounds[index], bounds[index + 1]) for index in range(0, len(bounds), 2)]
    prefix = ""
    first = _WORD_RE.finditer(question, *segments[0])
    if "and" not in cuts[0].group().lower() and not any(
        _OPENS_RE.match(question, word.start()) for word in first
    ):
        prefix = question[: cuts[0].end()]
        segments.pop(0)
    if len(segments) < 2:
        return None
    shared = ((0, len(prefix)),) if prefix else ()
    parts = []
    for low, high in segments:
        text = question[low:high].rstrip(" ?.!")
        parts.append(QuestionPart(prefix + text, (*shared, (low, low + len(text)))))
    if len(parts) > MAX_PARTS:
        return QuestionSplit(tuple(parts), "too_many_parts", tuple(range(1, len(parts) + 1)))
    framing = _INTENT_STOPWORDS | _FRAMING_WORDS
    windowed, grouped = [], []
    for number, part in enumerate(parts, start=1):
        own = part.text[len(prefix) :]
        windows = _time_window(own).spans
        words = [
            word.group().lower()
            for word in _WORD_RE.finditer(own)
            if not any(low <= word.start() < high for low, high in windows)
        ]
        pairs = set(zip(words, words[1:], strict=False))
        if number > 1 and (_BACK_REFERENCES & set(words) or _BACK_REFERENCE_PHRASES & pairs):
            return QuestionSplit(tuple(parts), "dependent_part", (number,))
        if all(word in framing or word.isdigit() for word in words):
            return QuestionSplit(tuple(parts), "part_without_subject", (number,))
        windowed.append(bool(windows))
        grouped.append(_asks_grouping(config, own))
    for stated, in_prefix, hold in (
        (windowed, bool(_time_window(prefix).spans), "part_without_window"),
        (grouped, _asks_grouping(config, prefix), "part_without_grouping"),
    ):
        if any(stated) and not all(stated) and not in_prefix:
            missing = tuple(number for number, has in enumerate(stated, start=1) if not has)
            return QuestionSplit(tuple(parts), hold, missing)
    return QuestionSplit(tuple(parts))
