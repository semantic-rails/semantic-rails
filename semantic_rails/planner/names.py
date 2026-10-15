"""Plan's one warehouse read: a name the question gives a row, looked up in display names.

"How many new accounts did Acme have last month?" names an account by its name, which no package
declares as a value. The invariant: a draft filters on a row the question names only when exactly
one row, of the entities the draft's subject reaches that have a text ``display`` dimension the
caller sees, has a display holding the name's words, among the rows the caller may read, on a
lookup that came back complete. ``name_filters`` is the one place a draft gets that filter: the
row's key (``is``), and its display in ``group_by`` so the answer names it. Several such rows ask
which; none, or a lookup that fails or may have missed a row, leaves the draft as it was, for
readiness to hold.

A name is a run of capitalized words, or quoted text, that the draft would otherwise be held on
(``intent_holds._dropped_value_why``). Each is looked up once per request and entity, by one
select-less call through ``Runtime.query`` under the caller's identity, so row filters and
policies apply; readiness reads what was found (``honored_names``) and never looks again.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from .. import visible_view
from ._base import QUOTED, _name_matches
from .call import Call, QueryBuilder, condition
from .coverage import CoverageGap, _clarify_why, _referenced_ids
from .groupings import _display_entities, _entity_stand_ins

_WORD_RE = re.compile(r"[^\W_]+")
_POSSESSIVE_RE = re.compile(r"['’]s(?![^\W_])", re.IGNORECASE)
# The text before a sentence's first word: nothing, or the end of another sentence.
_SENTENCE_START_RE = re.compile(r"(?:^|[.?!:;])[\s\"'“‘(]*$")
# The most rows one lookup reads. A full read may have missed a row, so it never makes one match.
_LOOKUP_ROWS = 6
_P = ParamSpec("_P")
_R = TypeVar("_R")


@dataclass(frozen=True)
class NamedRow:
    """A row a name in the question finds: where the question says the name, and the row."""

    span: tuple[int, int]
    said: str
    entity: Any
    key_dimension: str
    # The row's value of its key dimension.
    value: Any
    display_dimension: str
    display: str

    def shown(self) -> str:
        return f"{self.entity.label or self.entity.id} {self.display} ({self.value})"

    def read(self, config: Any, query: Call) -> QueryBuilder:
        """``query`` filtered to this row by its key and grouped by its display."""
        builder = QueryBuilder(config, query).where(self.key_dimension, "is", self.value)
        return builder.group(self.display_dimension)


# A lookup's (key, display) rows holding the name, and whether the read was whole; None failed.
_Read = tuple[list[tuple[Any, str]], bool] | None


@dataclass
class _Lookups:
    """One request's lookups, by key dimension and name, and the rows each question names."""

    reads: dict[tuple[str, tuple[str, ...]], _Read] = field(default_factory=dict)
    named: dict[str, tuple[NamedRow, ...]] = field(default_factory=dict)
    question: str = ""
    unknown_words: Callable[[Call], Iterable[str]] | None = None


@dataclass(frozen=True)
class _Run:
    words: tuple[str, ...]
    said: str
    span: tuple[int, int]


_lookups: ContextVar[_Lookups | None] = ContextVar("planner_name_lookups", default=None)


def name_question(
    question: str | None = None,
    *,
    unknown_words: Callable[[Call], Iterable[str]] | None = None,
) -> str:
    """Retain each question's case before composition normalizes its words."""

    state = _lookups.get()
    if state is None:
        return question or ""
    if question is not None:
        state.question = question
        state.unknown_words = unknown_words
    return state.question


def name_unknown_words(query: Call) -> Iterable[str]:
    """Use plan's unknown-word check without coupling value generation to readiness."""

    state = _lookups.get()
    return state.unknown_words(query) if state is not None and state.unknown_words else ()


def with_name_lookups(operation: Callable[_P, _R]) -> Callable[_P, _R]:
    """Look each name up at most once in a request; without this scope plan looks none up."""

    @wraps(operation)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        if _lookups.get() is not None:
            return operation(*args, **kwargs)
        token = _lookups.set(_Lookups())
        try:
            return operation(*args, **kwargs)
        finally:
            _lookups.reset(token)

    return wrapped


def _runs(
    question: str, held: set[str], metric_spans: Iterable[tuple[int, int]] = ()
) -> list[_Run]:
    """Quoted text holding a held word, and each complete run of capitalized words side by side
    holding one, with the possessive "'s" after it. Every word of a run is its name: "Acme
    Accounts" is looked up whole, even though "Accounts" alone is a catalog word. An unquoted
    run leaves out the requested metric at either edge ("Acme MRR" names "Acme").

    A sentence capitalizes its first word, so a run there leaves it out unless it is held
    ("Did Acme …" is "Acme"), and one word there is a name only with a possessive ("Acme's
    MRR") or a capital past its first letter ("ACME"); "Roughly how many …" is none.
    """

    quoted = [match.span() for match in re.finditer(QUOTED, question)]
    runs: list[_Run] = []
    for start, end in quoted:
        inner = question[start + 1 : end - 1]
        words = tuple(word.lower() for word in _WORD_RE.findall(inner))
        if set(words) & held:
            runs.append(_Run(words, inner.strip(), (start, end)))
    current: list[re.Match[str]] = []

    def close() -> None:
        first = bool(current) and bool(_SENTENCE_START_RE.search(question[: current[0].start()]))
        if first and len(current) > 1 and current[0].group().lower() not in held:
            del current[0]
            first = False
        # Trim only complete declared names of selected metrics/measures, never individual
        # catalog words inside a row name ("Accounts" in "Acme Accounts"). Quotes stay whole.
        for low, high in metric_spans:
            if current and low == current[0].start():
                current[:] = [match for match in current if match.start() >= high]
            elif current and high == current[-1].end():
                current[:] = [match for match in current if match.end() <= low]
        words = tuple(match.group().lower() for match in current)
        if current and set(words) & held:
            start, end = current[0].start(), current[-1].end()
            tail = _POSSESSIVE_RE.match(question, end)
            if (
                tail
                or len(current) > 1
                or any(char.isupper() for char in current[0].group()[1:])
                or not first
            ):
                runs.append(_Run(words, question[start:end], (start, tail.end() if tail else end)))
        current.clear()

    for match in _WORD_RE.finditer(question):
        word = match.group()
        if any(start <= match.start() < end for start, end in quoted) or not word[0].isupper():
            close()
            continue
        if current and not question[current[-1].end() : match.start()].isspace():
            close()
        current.append(match)
    close()
    return sorted(runs, key=lambda run: run.span)


def _named_entities(runtime: Any, query: Call) -> list[tuple[Any, str, str]]:
    """Each entity the draft's subject reaches with one key dimension and a text display the
    caller sees, as (entity, key dimension, display dimension)."""

    config = runtime._config
    out: list[tuple[Any, str, str]] = []
    for entity in _display_entities(runtime, query):
        keys, shown = _entity_stand_ins(config, entity)
        display = next(row for row in config.dimensions if row.id == shown[0])
        if display.data_type == "string":
            out.append((entity, keys[0], display.id))
    return out


def _lookup(
    runtime: Any, key_dimension: str, display_dimension: str, words: tuple[str, ...]
) -> _Read:
    """The (key, display) rows whose display holds the words, and whether the read was whole
    (it came back short of its limit); None when it failed."""

    try:
        # The caller plan answers; without one, no row filter could apply, so nothing is read.
        pinned = visible_view.pinned_view(runtime)
        if pinned is None:
            return None
        # A select-less call lists the rows: each word, in any case, is a superset of the rows
        # holding the words side by side as words.
        builder = QueryBuilder(runtime._config).groups([display_dimension, key_dimension])
        for word in dict.fromkeys(words):
            builder.where(display_dimension, "contains", word)
        call = builder.order(display_dimension).order(key_dimension).limit(_LOOKUP_ROWS).build()
        rows = runtime.query(call.to_call(), context=dict(pinned.policy_context))["rows"]
    except Exception:  # noqa: BLE001 — a lookup that fails (no adapter, a denial) is the hold
        return None
    found = [
        (row[key_dimension], str(row[display_dimension]))
        for row in rows
        if row.get(key_dimension) is not None
        and row.get(display_dimension) is not None
        and _holds(str(row[display_dimension]), words)
    ]
    return found, len(rows) < _LOOKUP_ROWS


def _holds(display: str, words: tuple[str, ...]) -> bool:
    """Whether the display spells the words side by side, as whole words, in any case."""

    said = tuple(word.lower() for word in _WORD_RE.findall(display))
    return any(said[index : index + len(words)] == words for index in range(len(said)))


def name_filters(
    runtime: Any, question: str, query: Call, held: Iterable[str]
) -> tuple[Call, dict[str, Any] | None]:
    """The draft filtered to the one row each name in the question gives, or why plan asks.

    ``held`` are the words readiness would hold the draft on. A name naming several rows (of one
    entity or of several) returns the draft with a why that lists them, when it is the question's
    only name and its words are all ``held`` holds; the why offers no call to run (each would
    read one row and nothing else plan must still settle). The draft comes back unchanged when
    no name finds exactly one row on a whole read, when the names give two rows of one entity
    ("Acme and Globex"), which one filter can't read, or when an ambiguous name has company.
    """

    held = set(held)
    state = _lookups.get()
    selected = set(_referenced_ids(query))
    metric_spans = sorted(
        {
            (start, end)
            for row in [*runtime._config.measures, *runtime._config.metric_recipes]
            if row.id in selected
            for _, start, end in _name_matches(row, question.lower())
        },
        key=lambda span: span[1] - span[0],
        reverse=True,
    )
    runs = _runs(question, held, metric_spans) if state is not None else []
    entities = _named_entities(runtime, query) if runs else []
    if state is None or not entities:
        return query, None
    named: list[NamedRow] = []
    asks: list[tuple[list[NamedRow], bool]] = []
    for run in runs:
        matches: list[NamedRow] = []
        whole = True
        for entity, key_dimension, display_dimension in entities:
            cached = (key_dimension, run.words)
            if cached not in state.reads:
                state.reads[cached] = _lookup(runtime, key_dimension, display_dimension, run.words)
            read = state.reads[cached]
            if read is None:
                break
            whole = whole and read[1]
            matches.extend(
                NamedRow(run.span, run.said, entity, key_dimension, key, display_dimension, display)
                for key, display in read[0]
            )
        else:
            if len(matches) > 1:
                asks.append((matches, whole))
            elif matches and whole:
                named.extend(matches)
    if asks:
        # One name, the only words held, asks which row it is; with another name or word beside
        # it, a choice of row would still leave the question held, so plan holds.
        alone = len(runs) == 1 and set(held) <= set(runs[0].words)
        return query, _ask_why(asks) if alone else None
    rows = {(row.key_dimension, repr(row.value)) for row in named}
    if not named or len(rows) > len({row.key_dimension for row in named}):
        return query, None
    for row in named:
        query = row.read(runtime._config, query).build()
    state.named[question.lower()] = tuple(named)
    return query, None


def _ask_why(asks: list[tuple[list[NamedRow], bool]]) -> dict[str, Any]:
    """Why plan asks which row a name means, listing each row it found."""

    gaps = [
        CoverageGap(
            kind="name_ambiguous",
            clause=matches[0].said,
            message=(
                f"'{matches[0].said}' names {'' if whole else 'at least '}{len(matches)} rows: "
                f"{', '.join(match.shown() for match in matches)}."
            ),
            expected={
                "matches": [
                    {"entity": match.entity.id, "display": match.display, "key": match.value}
                    for match in matches
                ],
                **({} if whole else {"complete": False}),
            },
            recovery_hint={
                "kind": "name_one_row",
                "message": (
                    "Ask the user which one they mean, then plan again with its full name "
                    "(quoted, when it has words the catalog uses)."
                ),
            },
        )
        for matches, whole in asks
    ]
    matches = asks[0][0]
    question = (
        f"Which one does '{matches[0].said}' mean: "
        f"{' or '.join(match.shown() for match in matches)}?"
    )
    return _clarify_why(gaps, {"question": question})


def honored_names(question: str, query: Call) -> tuple[NamedRow, ...]:
    """The rows plan found for the question's names that the draft reads: it filters on the
    row's key and groups by its display. Readiness consumes their spans; nothing is looked up."""

    state = _lookups.get()
    if state is None:
        return ()
    return tuple(
        row
        for row in state.named.get(str(question or "").lower(), ())
        if row.display_dimension in query.group_by
        and condition(row.key_dimension, "is", row.value) in query.conditions
    )


def without_named_rows(question: str, query: Call) -> str:
    """Value matching sees only text outside confirmed row names, retaining its offsets.

    Composition and faithfulness can substitute metric IDs or mask subjects in their text;
    only the confirmed name's complete, unchanged spelling is shielded in that text.
    """

    text = question
    source = name_question() or question
    for row in honored_names(source, query):
        start, end = row.span
        spelling = re.escape(source[start:end])
        text = re.sub(
            rf"(?<!\w){spelling}(?!\w)",
            lambda match: " " * len(match.group()),
            text,
            flags=re.IGNORECASE,
        )
    return text


def name_readings(question: str, query: Call) -> list[str]:
    """An assumption line for each row a name in the question is read as."""

    return [
        f"'{row.said}' is read as {row.entity.label or row.entity.id} '{row.display}'."
        for row in honored_names(question, query)
    ]


__all__ = ["NamedRow", "honored_names", "name_filters", "name_readings", "with_name_lookups"]
