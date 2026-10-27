"""Clock phrases are found in one pass over the question, whatever its length."""

from __future__ import annotations

import random
import re
import time

import pytest

from semantic_rails.planner import grouping_checks
from semantic_rails.planner.groupings import _is_temporal_grouping_term
from semantic_rails.planner.time_phrases import _names_time_axis

VOCABULARY = (
    *("day", "days", "daily", "week", "weekly", "month", "monthly", "quarter", "year"),
    *("time", "date", "dates", "delivered", "ordered", "order", "orders", "at", "first"),
    *("level", "levels", "grain", "grains", "revenue", "store", "by", "and", "x", "customer"),
    *("session", "sessions", "start"),
)
SEPARATORS = (" ", " ", " ", "  ", ", ", "-", "_", " / ", "\t")
LABELS = ("Order time", "Customer first order date", "Session start time", "Delivered at", "")


def _every_clock_phrase(lowered: str, clocks: list[str]) -> list[tuple[int, int]]:
    """Every clock phrase, by checking every run of words: quadratic, as a reference."""
    words = list(re.finditer(r"[^\W_]+", lowered))
    spans = []
    for index, first in enumerate(words):
        for last in words[index:]:
            text = lowered[first.start() : last.end()]
            if grouping_checks._LEVEL_WORD_RE.search(text) or not re.fullmatch(
                r"[^\W_]+(?:\s+[^\W_]+)*", text
            ):
                break
            if _is_temporal_grouping_term(text) or any(_names_time_axis(text, c) for c in clocks):
                spans.append((first.start(), last.end()))
    return spans


def _read(spans: list[tuple[int, int]]) -> tuple[set[tuple[int, int]], set[int]]:
    """What callers read: the phrases no other phrase contains, and where phrases end."""
    widest = {
        span
        for span in spans
        if not any(other != span and other[0] <= span[0] and span[1] <= other[1] for other in spans)
    }
    return widest, {end for _, end in spans}


def _clock_spans(monkeypatch: pytest.MonkeyPatch, question: str, label: str):
    clocks = [label] if label else []
    monkeypatch.setattr(grouping_checks, "_query_clocks", lambda _config, _query: clocks)
    return grouping_checks._clock_spans(None, question, None), clocks


def test_one_pass_reads_the_same_phrases_as_every_run_of_words(monkeypatch):
    rng = random.Random(20261027)
    for _ in range(3000):
        words = rng.randint(1, 12)
        question = "".join(rng.choice(VOCABULARY) + rng.choice(SEPARATORS) for _ in range(words))
        question = question.strip()
        spans, clocks = _clock_spans(monkeypatch, question, rng.choice(LABELS))
        reference = _every_clock_phrase(question, clocks)
        assert set(spans) <= set(reference), question
        assert _read(spans) == _read(reference), (question, clocks)


@pytest.mark.parametrize(
    ("question", "label", "expected"),
    [
        ("revenue by ordered month level", "", [(11, 24)]),  # a fixed term
        ("revenue by order date and week", "Order time", [(11, 21), (26, 30)]),
        ("revenue by day week, month", "", [(11, 14), (11, 19), (21, 26)]),
        ("store name levels by month", "", [(21, 26)]),
    ],
)
def test_the_longest_phrase_ending_at_each_word(monkeypatch, question, label, expected):
    assert _clock_spans(monkeypatch, question, label)[0] == expected


@pytest.mark.parametrize("phrase", ["x", "day", "order date", "day week month"])
def test_a_thousand_word_question_takes_one_pass(monkeypatch, phrase):
    question = " ".join([phrase] * (1000 // len(phrase.split())))
    started = time.perf_counter()
    spans, _ = _clock_spans(monkeypatch, question, "Order time")
    # Under a second on a busy runner; the quadratic reading took about 4 s per question.
    assert time.perf_counter() - started < 1.0
    assert len(spans) <= 1000
