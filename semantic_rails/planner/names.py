"""Plan's one warehouse read: a name the question gives a row, looked up in display names.

"How many new accounts did Acme have last month?" names an account by its name, which no package
declares as a value. The invariant: a draft filters on a row the question names only when exactly
one row, of the entities the draft's subject reaches that have a ``display`` dimension the caller
sees, has a display holding the name's words, among the rows the caller may read.
``name_filters`` is the one place a draft gets that filter: the row's key, and its display in
``group_by`` so the answer names it. Several such rows ask which; none, or a lookup that fails or
may have missed a row, leaves the draft as it was, for readiness to hold.

A name is a run of capitalized words, or quoted text, that the draft would otherwise be held on
(``intent_holds._dropped_value_why``). Each is looked up once per request and entity, by one
bounded ``Runtime.query`` under the caller's policy context, so row filters and policies apply;
readiness reads what was found (``honored_names``) and never looks again.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from .. import visible_view
from ..catalog_search import search_pattern
from ._base import QUOTED, _object_by_id
from .coverage import CoverageGap, _coverage_why
from .groupings import _entity_stand_ins
from .plan_query import _where_filters

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
    """A name the question gives one row: where the question says it, and the row it reads."""

    span: tuple[int, int]
    said: str
    entity: str
    key_dimension: str
    key: Any
    display_dimension: str
    display: str


@dataclass
class _Lookups:
    """One request's lookups, by key dimension and name, and the rows each question names."""

    reads: dict[tuple[str, tuple[str, ...]], tuple[list[tuple[Any, str]], bool] | None] = field(
        default_factory=dict
    )
    named: dict[str, tuple[NamedRow, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class _Run:
    words: tuple[str, ...]
    said: str
    span: tuple[int, int]


@dataclass(frozen=True)
class _Match:
    entity: Any
    key_dimension: str
    display_dimension: str
    key: Any
    display: str


_lookups: ContextVar[_Lookups | None] = ContextVar("planner_name_lookups", default=None)


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


def _runs(question: str, held: set[str]) -> list[_Run]:
    """Quoted text holding a held word, and each longest run of capitalized held words side by
    side, with the possessive "'s" after it.

    A sentence capitalizes its first word, so one word there is a name only with a possessive
    ("Acme's MRR") or a capital past its first letter ("ACME"); "Roughly how many …" is none.
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
        if current:
            start, end = current[0].start(), current[-1].end()
            tail = _POSSESSIVE_RE.match(question, end)
            words = tuple(match.group().lower() for match in current)
            if (
                tail
                or len(current) > 1
                or any(char.isupper() for char in current[0].group()[1:])
                or not _SENTENCE_START_RE.search(question[:start])
            ):
                runs.append(_Run(words, question[start:end], (start, tail.end() if tail else end)))
            current.clear()

    for match in _WORD_RE.finditer(question):
        word = match.group()
        if (
            any(start <= match.start() < end for start, end in quoted)
            or not word[0].isupper()
            or word.lower() not in held
        ):
            close()
            continue
        if current and not question[current[-1].end() : match.start()].isspace():
            close()
        current.append(match)
    close()
    return sorted(runs, key=lambda run: run.span)


def _display_entities(config: Any, query: dict[str, Any]) -> list[tuple[Any, str, str]]:
    """Each entity the draft's subject reaches with one key dimension and a text display the
    caller sees, as (entity, key dimension, display dimension)."""

    from ..metadata import _availability_for_object, _selection_context  # noqa: WPS433

    try:
        root = _selection_context(config, query)["root_entity"]
    except Exception:  # noqa: BLE001 — a subject plan can't read reaches no entity
        return []
    out: list[tuple[Any, str, str]] = []
    for entity in config.entities if root else []:
        keys, shown = _entity_stand_ins(config, entity)
        display = _object_by_id(config.dimensions, shown[0]) if shown else None
        if (
            len(keys) == 1
            and display is not None
            and display.id == entity.display
            and display.data_type == "string"
            and _availability_for_object(config, root, entity.id, "entity")["available"]
        ):
            out.append((entity, keys[0], display.id))
    return out


def _lookup(
    runtime: Any, key_dimension: str, display_dimension: str, words: tuple[str, ...]
) -> tuple[list[tuple[Any, str]], bool] | None:
    """The (key, display) rows whose display holds the words, and whether the read was whole
    (it came back short of its limit); None when it failed."""

    payload: dict[str, Any] = {
        "version": 1,
        "select": [],
        "observation_scope": "query",
        "group_by": [display_dimension, key_dimension],
        "where": [
            {"field": display_dimension, "op": "ILIKE", "value": search_pattern(" ".join(words))}
        ],
        "order_by": [
            {"field": display_dimension, "direction": "ASC"},
            {"field": key_dimension, "direction": "ASC"},
        ],
        "limit": _LOOKUP_ROWS,
    }
    try:
        # The caller plan answers; without one, no row filter could apply, so nothing is read.
        pinned = visible_view.pinned_view(runtime)
        if pinned is None:
            return None
        rows = runtime.query({**payload, "policy_context": dict(pinned.policy_context)})["rows"]
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
    runtime: Any, question: str, query: dict[str, Any], held: Iterable[str]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The draft filtered to the one row each name in the question gives, or why plan asks.

    ``held`` are the words readiness would hold the draft on. A name naming several rows (of one
    entity or of several) returns the draft with a why that lists them. The draft comes back
    unchanged when no name finds exactly one row on a whole read, or when the names give two rows
    of one entity ("Acme and Globex"), which one filter can't read.
    """

    state = _lookups.get()
    runs = _runs(question, set(held)) if state is not None else []
    entities = _display_entities(runtime._config, query) if runs else []
    if state is None or not entities:
        return query, None
    named: list[NamedRow] = []
    asks: list[tuple[_Run, list[_Match], bool]] = []
    for run in runs:
        matches: list[_Match] = []
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
                _Match(entity, key_dimension, display_dimension, key, display)
                for key, display in read[0]
            )
        else:
            if len(matches) > 1:
                asks.append((run, matches, whole))
            elif matches and whole:
                [match] = matches
                named.append(
                    NamedRow(
                        span=run.span,
                        said=run.said,
                        entity=str(match.entity.label or match.entity.id),
                        key_dimension=match.key_dimension,
                        key=match.key,
                        display_dimension=match.display_dimension,
                        display=match.display,
                    )
                )
    if asks:
        return query, _ask_why(asks)
    keys: dict[str, set[str]] = {}
    for row in named:
        keys.setdefault(row.key_dimension, set()).add(repr(row.key))
    if not named or any(len(found) > 1 for found in keys.values()):
        return query, None
    where = list(query.get("where") or [])
    for row in named:
        condition = {"field": row.key_dimension, "op": "=", "value": row.key}
        if condition not in where:
            where.append(condition)
    group_by = [*(query.get("group_by") or []), *(row.display_dimension for row in named)]
    state.named[question.lower()] = tuple(named)
    return {**query, "where": where, "group_by": list(dict.fromkeys(group_by))}, None


def _ask_why(asks: list[tuple[_Run, list[_Match], bool]]) -> dict[str, Any]:
    """Why plan asks which row a name means, listing each row it found."""

    def shown(match: _Match) -> str:
        return f"{match.entity.label or match.entity.id} {match.display} ({match.key})"

    gaps = [
        CoverageGap(
            kind="name_ambiguous",
            clause=run.said,
            message=(
                f"'{run.said}' names {'' if whole else 'at least '}{len(matches)} rows: "
                f"{', '.join(shown(match) for match in matches)}."
            ),
            expected={
                "matches": [
                    {
                        "entity": match.entity.id,
                        "display": match.display,
                        "key": match.key,
                        "where": {"field": match.key_dimension, "op": "=", "value": match.key},
                        "group_by": match.display_dimension,
                    }
                    for match in matches
                ],
                **({} if whole else {"complete": False}),
            },
            recovery_hint={
                "kind": "name_one_row",
                "message": (
                    "Ask the user which one they mean, then add its where filter to "
                    "best.query_ir and its display to group_by, and validate; or plan again "
                    "with its full name."
                ),
            },
        )
        for run, matches, whole in asks
    ]
    why = _coverage_why(gaps) or {}
    run, matches, _whole = asks[0]
    question = f"Which one does '{run.said}' mean: {' or '.join(map(shown, matches))}?"
    return {**why, "details": {**why.get("details", {}), "clarification": {"question": question}}}


def honored_names(question: str, query: dict[str, Any]) -> tuple[NamedRow, ...]:
    """The rows plan found for the question's names that the draft reads: it filters on the
    row's key and groups by its display. Readiness consumes their spans; nothing is looked up."""

    state = _lookups.get()
    if state is None:
        return ()
    where = _where_filters(query)
    grouped = set(query.get("group_by") or [])
    return tuple(
        row
        for row in state.named.get(str(question or "").lower(), ())
        if row.display_dimension in grouped
        and {"field": row.key_dimension, "op": "=", "value": row.key} in where
    )


def name_readings(question: str, query: dict[str, Any]) -> list[str]:
    """An assumption line for each row a name in the question is read as."""

    return [
        f"'{row.said}' is read as {row.entity} '{row.display}'."
        for row in honored_names(question, query)
    ]


__all__ = ["NamedRow", "honored_names", "name_filters", "name_readings", "with_name_lookups"]
