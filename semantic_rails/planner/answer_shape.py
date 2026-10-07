"""Readiness: the result has the rows, comparison and values the question's shape asks for."""

from __future__ import annotations

import re
from typing import Any

from ._base import _TERM_SYNONYMS, _last_token, _object_by_id, _singular
from .consumed_spans import _TERM_RE, _name_spans
from .coverage import CoverageGap, _coverage_why, _query_contains_prior_period
from .grouping_checks import (
    _declared_name_spans,
    _entity_grouping_dimensions,
    _reads_grouping,
    _time_of,
)
from .groupings import _listed_grouping_terms, _requested_grouping_spans
from .plan_query import _select_key
from .time_windows import _time_window
from .unasked_groupings import _grain_splits
from .unmatched_words import _FRAMING_WORDS

# Words asking for more than one value (``_answer_shape_why``). "per" is not one: "revenue per
# order" is a ratio.
_PERSON_WORDS = frozenset({"who", "whom", "whose"})
_LIST_WORDS = _PERSON_WORDS | {"which", "list"}
_EACH_WORDS = frozenset({"each", "every"})
_COMPARISON_WORDS = frozenset(
    {"against", "compare", "compared", "compares", "comparing", "comparison", "versus", "vs"}
)
_COMPARISON_PHRASE_RE = re.compile(r"\b(?:up\s+or\s+down|down\s+or\s+up)\b")
# A question asking for one value: "how many", "how much", "what is", "what was", "what's".
_VALUE_QUESTION_RE = re.compile(r"\bhow\s+(?:many|much)\b|\bwhat(?:['’]s|\s+(?:is|was|are|were))\b")
# Where a clause starts: after punctuation, or after "and" ("How many orders and who placed
# them?").
_CLAUSE_BREAK_RE = re.compile(r"[,;:.?!\n]|\band\b")


def _answer_shape_why(
    runtime: Any,
    question: str,
    query: dict[str, Any],
    partial_query: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The draft's result holds the part each of the question's shape words asks for, or the
    plan is not ready.

    "who", "whom" or "whose" opening a clause, or "which" or "list" opening one, asks for an
    entity's rows: the draft's group_by needs its declared key (``_lists_entity_rows``). "each"
    or "every" needs a row per item (a group_by, or a grain that splits the rows). A comparison
    word ("compared", "vs", "versus", "against", "up or down") needs a value to compare with: a
    prior-period select. A second select (which may spell the first one again), a group_by or a
    grain doesn't show what the question compares. Two or more questions for a value ("how
    many", "how much", "what is", "what was") need a select of their own each, which names what
    the question asks about (``_questions_answered``). A word opens a clause when
    every word before it in its clause (from punctuation or "and") is a stopword, a framing word
    or a word of a time window the question states ("show me which stores", "last week, who",
    "how many orders and who"); in "customers who ordered" it is a relative pronoun. A word
    inside a declared name asks nothing. The check only holds a plan: it never changes a draft
    or makes one ready.
    """

    from ..metadata_parts.relevance import _INTENT_STOPWORDS  # noqa: WPS433

    config = runtime._config
    lowered = str(question or "").lower()
    names = list(_declared_name_spans(config, lowered))
    windows = list(_time_window(question).spans)
    framing = _INTENT_STOPWORDS | _FRAMING_WORDS
    tokens = list(re.finditer(r"[^\W_]+", lowered))
    breaks = [match.start() for match in _CLAUSE_BREAK_RE.finditer(lowered)]

    def outside(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
        return not any(low <= span[0] and span[1] <= high for low, high in spans)

    def opens_clause(index: int) -> bool:
        clause = max((at for at in breaks if at < tokens[index].start()), default=-1)
        return all(
            (token.group() in framing and outside(token.span(), names))
            or not outside(token.span(), windows)
            for token in tokens[:index]
            if token.start() > clause
        )

    def clause_end(at: int) -> int:
        return min((end for end in breaks if end >= at), default=len(lowered))

    asked: dict[str, list[str]] = {"person": [], "list": [], "each": [], "comparison": []}
    unlisted: list[str] = []
    keys: set[str] = set()
    for index, token in enumerate(tokens):
        word = token.group()
        kind = (
            ("person" if word in _PERSON_WORDS else "list")
            if word in _LIST_WORDS and opens_clause(index)
            else "each"
            if word in _EACH_WORDS
            else "comparison"
            if word in _COMPARISON_WORDS
            else ""
        )
        if not kind or not outside(token.span(), names):
            continue
        if word not in asked[kind]:
            asked[kind].append(word)
        if kind in ("person", "list"):
            listed, wanted = _lists_entity_rows(
                config,
                lowered,
                windows,
                (token.end(), clause_end(token.end())),
                kind == "person",
                query,
                partial_query or {},
            )
            keys |= wanted
            if not listed and word not in unlisted:
                unlisted.append(word)
    for match in _COMPARISON_PHRASE_RE.finditer(lowered):
        if (phrase := " ".join(match.group().split())) not in asked["comparison"]:
            asked["comparison"].append(phrase)
    matches = list(_VALUE_QUESTION_RE.finditer(lowered))
    questions = [" ".join(match.group().split()) for match in matches]
    # What each question for a value asks about: the first run of words after it that are not
    # stopwords, framing words, numbers or a window's words ("orders" in "how many orders did
    # we get last week"), up to its clause's end or the next question.
    subjects: list[list[tuple[int, int]]] = []
    for index, match in enumerate(matches):
        stop = min([clause_end(match.end()), *(later.start() for later in matches[index + 1 :])])
        subject: list[tuple[int, int]] = []
        for token in tokens:
            if not (match.end() <= token.start() and token.end() <= stop):
                continue
            filler = (
                token.group() in framing
                or not outside(token.span(), windows)
                or any(char.isdigit() for char in token.group())
            )
            if filler and subject:
                break
            if not filler:
                subject.append(token.span())
        subjects.append(subject)

    values = [item for item in query.get("select") or [] if isinstance(item, dict)]
    split = bool(query.get("group_by")) or _grain_splits(_time_of(query))
    compared = _query_contains_prior_period(runtime, query)
    # (kind, the words asking, whether the draft's shape leaves them unanswered, message,
    # expected, hint kind, hint).
    shapes: list[tuple[str, list[str], bool, str, dict[str, Any], str, str]] = [
        (
            "list_unrealized",
            unlisted,
            bool(unlisted),
            "The question asks for the rows of what it lists ({words}), but the draft's "
            "group_by has no declared key of that entity, or plan can't tell which entity it "
            "lists, so it doesn't list them: a name can repeat across rows.",
            {"answer": "rows", **({"key_dimensions": sorted(keys)} if keys else {})},
            "group_by_listed_rows",
            "Find what the question lists with discover, add its key dimension to "
            "best.query_ir group_by (a name may go beside it), then validate; or ask the user "
            "what to list.",
        ),
        (
            "each_unrealized",
            asked["each"],
            bool(asked["each"]) and not split,
            "The question asks for a row per item ({words}), but the draft has no group_by or "
            "time grain that splits the rows, so it returns one total.",
            {"answer": "row per item"},
            "group_by_each_item",
            "Add the dimension or time grain the question asks for each of to best.query_ir, "
            "then validate; or ask the user what it means.",
        ),
        (
            "comparison_unrealized",
            asked["comparison"],
            bool(asked["comparison"]) and not compared,
            "The question asks for a comparison ({words}), but the draft has no prior-period "
            "select, so it has nothing to compare with: a second select, a group_by or a time "
            "grain doesn't say what the question compares.",
            {"answer": "values to compare"},
            "add_compared_value",
            "Add what the question compares with to best.query_ir (a prior_period select for "
            "an earlier period), then validate; or ask the user what to compare.",
        ),
        (
            "multiple_questions_unrealized",
            questions,
            len(questions) > 1 and not _questions_answered(config, lowered, subjects, values),
            f"The question asks {len(questions)} questions for a value ({{words}}), but the "
            "draft has no select of its own for each that names what it asks about.",
            {"select_count": len(questions)},
            "plan_each_question",
            "Plan each question on its own, or give best.query_ir one select per value asked "
            "for, then validate.",
        ),
    ]
    actual = {
        "select_count": len(values),
        "group_by": list(query.get("group_by") or []),
        "time_grain": _time_of(query).get("grain"),
    }
    gaps = []
    for kind, words, unmet, message, expected, hint, text in shapes:
        if unmet:
            clause = ", ".join(f'"{word}"' for word in words)
            gaps.append(
                CoverageGap(
                    kind=kind,
                    clause=clause,
                    message=message.format(words=clause),
                    expected=expected,
                    actual=actual,
                    recovery_hint={"kind": hint, "message": text},
                )
            )
    return _coverage_why(gaps)


def _lists_entity_rows(
    config: Any,
    lowered: str,
    windows: list[tuple[int, int]],
    clause: tuple[int, int],
    person: bool,
    query: dict[str, Any],
    caller: dict[str, Any],
) -> tuple[bool, set[str]]:
    """Whether the draft's group_by lists the rows a list or person word asks for, with the key
    dimensions that would list them.

    The rows are those of the entity the word's clause names: its first word, outside a window
    and a grouping the question lists ("by store"), that names an entity. With none, "list" or
    "which" lists the entity a grouping in its clause names ("List revenue by store"). Only the
    entity's declared one-column key lists them: the key among its stand-ins
    (``_entity_grouping_dimensions``). A display name may sit beside the key, but lists nothing
    on its own, because a name can repeat ("Customer name"); nor does any other dimension of the
    entity, whatever it declares. When "who" names no entity ("Who ordered last week?"), plan
    can't tell whose rows it asks for, so only the caller's group_by says: one of its dimensions
    that reads no grouping the question lists ("Who ordered by store?" asks for more than
    stores) and is the key of its own entity. A time grain, a category, a name or an entity the
    clause doesn't name never lists them, and when "list" or "which" names no entity, nothing
    does.
    """

    chosen = set(caller.get("group_by") or [])
    grouped = [
        row
        for item in dict.fromkeys(query.get("group_by") or [])
        if (row := _object_by_id(config.dimensions, item)) is not None
    ]

    def keys_of(term: str) -> set[str]:
        stand_ins = _entity_grouping_dimensions(config, term) or set()
        return {
            row.id
            for row in config.dimensions
            if row.id in stand_ins
            and (entity := _object_by_id(config.entities, row.entity)) is not None
            and row.column == entity.key[0]
        }

    groupings = _requested_grouping_spans(lowered)
    words = [
        match
        for match in re.finditer(r"[^\W_]+", lowered[: clause[1]])
        if match.start() >= clause[0]
        and not any(low <= match.start() < high for low, high in windows)
    ]

    def named(listed: bool) -> str | None:
        return next(
            (
                match.group()
                for match in words
                if any(low <= match.start() < high for low, high in groupings) is listed
                and _entity_grouping_dimensions(config, match.group()) is not None
            ),
            None,
        )

    term = named(False) or (None if person else named(True))
    if term is not None:
        keys = keys_of(term)
        return any(row.id in keys for row in grouped), keys
    if not person:
        return False, set()
    terms = _listed_grouping_terms(lowered, config)
    for row in grouped:
        entity = _object_by_id(config.entities, row.entity)
        if (
            row.id not in chosen
            or entity is None
            or any(
                _reads_grouping(term, _entity_grouping_dimensions(config, term), row)
                for term in terms
            )
        ):
            continue
        if row.id in keys_of(str(entity.label or _last_token(entity.name))):
            return True, set()
    return False, set()


def _questions_answered(
    config: Any, lowered: str, subjects: list[list[tuple[int, int]]], values: list[dict[str, Any]]
) -> bool:
    """Whether each question for a value has a select of its own that names what it asks about.

    ``subjects`` holds, per question, the spans of the words it asks about. A select names them
    when each lies where the question spells a whole name (label, alias, or the last part of
    its id or name) of the measure, at its declared aggregation, or the metric that the select's
    expression is. A clock, filter, grouping or description names nothing, nor does an
    expression built on an object (a ratio, a filtered or prior-period aggregate). Selects with
    one expression (``_select_key``) are one select, and each question needs another one.
    """

    tokens = [(match.group(0), *match.span()) for match in _TERM_RE.finditer(lowered)]
    normal = [_singular(_TERM_SYNONYMS.get(word, word)) for word, _start, _end in tokens]
    measures = {row.id: row for row in config.measures}
    metrics = {row.id: row for row in config.metric_recipes}
    named: dict[str, list[tuple[int, int]]] = {}
    for item in values:
        raw = item.get("expression")
        expression = raw if isinstance(raw, dict) else {}
        kind = expression.get("kind", "measure" if "measure" in expression else "metric")
        if kind in ("measure", "measure_ref"):
            row = measures.get(str(expression.get("measure")))
            declared = row is not None and expression.get("aggregation") in (
                None,
                "",
                row.default_aggregation,
            )
        else:
            row = metrics.get(str(expression.get("metric"))) if kind == "metric" else None
            declared = row is not None
        if row is None or not declared:
            continue
        aliases = [str(alias) for alias in row.aliases or []]
        names = [str(row.label or ""), *aliases, _last_token(row.id), _last_token(row.name)]
        spans = named.setdefault(_select_key(config, item), [])
        spans.extend(_name_spans(tokens, normal, names, whole=True))
    options = [
        [
            key
            for key, spans in named.items()
            if subject
            and all(
                any(low <= start and end <= high for low, high in spans) for start, end in subject
            )
        ]
        for subject in subjects
    ]
    owner: dict[str, int] = {}

    def assign(index: int, seen: set[str]) -> bool:
        # A question takes a free select, or one whose question can take another.
        for key in options[index]:
            if key not in seen:
                seen.add(key)
                if key not in owner or assign(owner[key], seen):
                    owner[key] = index
                    return True
        return False

    return all(assign(index, set()) for index in range(len(subjects)))
