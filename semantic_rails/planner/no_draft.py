"""When a held plan offers no draft: no measure the question names, or pieces that don't connect.

A held plan hands its draft back for the caller to fix. Two drafts are no help there, so plan
offers none and says why instead:

- the draft's measure or metric is named by no word the question asks about
  (``unmatched_subject_why``): "How many employees work at each store?" would draft the open
  store count, which shares only "store", a word the grouping already says. The words the
  question asks about are its words outside a grouping it asks for and a time window, and
  outside stopwords, aggregation words ("count", "total", "average"), units of time and
  numbers. When none of them names a subject of the draft and some name nothing in the
  catalog's dimensions, entities and values either, plan names those words, with the measures
  and metrics whose names or descriptions say one of them. A subject that shares any of the
  words is named, even by an entity's name ("customer satisfaction score" names the customer
  count): a limitation, never a wrong answer, since that draft stays held;
- the draft fails validation because its measure can't reach a dimension it reads
  (``unconnected_why``): plan names the pair and the engine's refusal.

Both only ever replace a plan that was not ready: a ready plan never changes.
"""

from __future__ import annotations

import re
from typing import Any

from ._base import _TERM_SYNONYMS, _last_token, _object_by_id, _object_text, _singular, _tokens
from .call import Call
from .coverage import _value_phrases
from .intent_ir import _SUBJECT_STOPWORDS
from .time_phrases import _TIME_UNITS
from .time_windows import _time_window
from .unasked_groupings import _asked_grouping_terms
from .unmatched_words import _FRAMING_WORDS
from .visibility import visible_dimensions

# Words that say how to aggregate or count, never what: no measure is named by them alone.
_AGGREGATION_WORDS = frozenset(
    {"average", "avg", "count", "mean", "number", "sum", "total", "value", "amount"}
)
# Refusals that say the draft's measure can't reach a dimension it reads.
_UNCONNECTED_CODES = frozenset({"PATH_NOT_FOUND", "MIXED_GRAIN_INVALID"})
# The catch-all patterns, which draft on the best-scoring measure whatever the question's shape.
_CATCH_ALLS = frozenset({"metric_by_dimension_rollup", "catalog_fallback"})
# A fallback draft shown on a subject the question doesn't name says so in its trace.
PLACEHOLDER = (
    "No measure or metric the question names matched: this draft is a placeholder on the "
    "closest catalog object, so don't run it."
)


def _normal(word: str) -> str:
    return _singular(_TERM_SYNONYMS.get(word, word))


def _names(row: Any) -> set[str]:
    """The words of an object's own names (label, id, name, synonyms), never its description."""

    text = " ".join(
        [
            re.sub(r"\s*\(.*?\)", "", str(getattr(row, "label", "") or "")),
            _last_token(str(getattr(row, "id", ""))),
            _last_token(str(getattr(row, "name", "") or "")),
            *(getattr(row, "aliases", None) or []),
        ]
    )
    return {_normal(word) for word in _tokens(text)}


def _asked_words(config: Any, question: str) -> list[tuple[str, bool]]:
    """The words the question asks about (see the module docstring), as it spells them, each
    with whether it names nothing in the catalog's dimensions, entities and values."""

    from ..metadata_parts.relevance import _INTENT_STOPWORDS  # noqa: WPS433

    lowered = question.lower()
    windows = _time_window(question).spans
    skipped = _INTENT_STOPWORDS | _FRAMING_WORDS | _SUBJECT_STOPWORDS | _AGGREGATION_WORDS
    grouped = {
        _normal(word) for term in _asked_grouping_terms(config, question) for word in _tokens(term)
    }
    catalog = {
        _normal(word)
        for row in [*visible_dimensions(config), *config.entities]
        for word in _names(row)
    } | {_normal(word) for phrase in _value_phrases(config) for word in _tokens(phrase)}
    words: dict[str, bool] = {}
    for match in re.finditer(r"[^\W_]+", lowered):
        word = match.group()
        normal = _normal(word)
        if not (
            any(start <= match.start() < end for start, end in windows)
            or word in skipped
            or normal in skipped
            or normal in grouped
            or normal in _TIME_UNITS
            or any(char.isdigit() for char in word)
        ):
            words[word] = normal not in catalog
    return list(words.items())


def unnamed_subject_terms(config: Any, question: str, query: Call) -> list[str]:
    """The words the question asks about that name nothing in the catalog, when no subject of
    the draft is named by any word it asks about; empty otherwise."""

    words = _asked_words(config, question)
    rows = [
        row
        for ref in query.references()
        if (row := _object_by_id([*config.measures, *config.metric_recipes], ref)) is not None
    ]
    said = {_normal(word) for word, _unknown in words}
    if not rows or any((_names(row) - _AGGREGATION_WORDS) & said for row in rows):
        return []
    return [word for word, unknown in words if unknown]


def unmatched_subject_why(
    runtime: Any, question: str, row: dict[str, Any], partial_query: Call | None
) -> dict[str, Any] | None:
    """Why plan offers no draft on a subject the question doesn't name, or None.

    Only a catch-all pattern's draft is read, and never one whose select the caller chose.
    """

    config = runtime._config
    if row["pattern"] not in _CATCH_ALLS or (partial_query is not None and partial_query.select):
        return None
    terms = unnamed_subject_terms(config, question, row["draft"].query)
    if not terms:
        return None
    said = {_normal(word) for word in terms}
    candidates = [
        {"id": item.id, "label": item.label}
        for item in sorted(
            [*config.measures, *config.metric_recipes], key=lambda item: (item.label, item.id)
        )
        if said & {_normal(word) for word in _tokens(_object_text(item))}
    ][:5]
    closest = (
        "The closest catalog objects mention them: "
        + ", ".join(f"{item['label']} ({item['id']})" for item in candidates)
        + "; plan again naming one of them, or ask the user."
        if candidates
        else "Nothing in the catalog mentions them, so the package may not hold this."
    )
    return {
        "code": "PLAN_SUBJECT_UNMATCHED",
        "message": (
            f"No measure or metric matches what the question asks about ({', '.join(terms)}), "
            f"so plan offers no draft on an unrelated one. {closest}"
        ),
        "details": {"terms": terms, "candidates": candidates},
        "recovery_hints": [
            {
                "kind": "name_the_measure",
                "message": (
                    "Ask again naming a measure or metric discover lists, or ask the user what "
                    "the words mean."
                ),
            }
        ],
    }


def unconnected_why(
    runtime: Any, row: dict[str, Any], partial_query: Call | None
) -> dict[str, Any] | None:
    """Why plan offers no draft whose measure can't reach a dimension it reads, or None.

    A dimension the caller's ``partial_query`` groups or filters by is the caller's call: its
    refusal stays the validation why, with the call as the draft.
    """

    config = runtime._config
    error = next(
        (
            item
            for item in row.get("validation", {}).get("errors") or []
            if isinstance(item, dict) and item.get("code") in _UNCONNECTED_CODES
        ),
        None,
    )
    if row.get("blocked") or error is None:
        return None
    details = error.get("details") or {}
    target = str(details.get("target") or details.get("target_entity") or "")
    query: Call = row["draft"].query
    dimensions = [
        field
        for field in dict.fromkeys([*query.group_by, *(item.field for item in query.conditions)])
        if (dimension := _object_by_id(config.dimensions, field)) is not None
        and (not target or dimension.entity == target)
    ]
    subjects = [ref for ref in query.references() if ref.startswith(("measure.", "metric."))]
    caller = set(partial_query.references()) if partial_query is not None else set()
    if not subjects or not dimensions or caller & set(dimensions):
        return None

    def shown(ids: list[str], rows: list[Any]) -> str:
        return ", ".join(
            f"{getattr(_object_by_id(rows, item), 'label', item)} ({item})" for item in ids
        )

    entity = _object_by_id(config.entities, target)
    reached = f" on {entity.label}" if entity is not None else ""
    return {
        "code": "PLAN_UNCONNECTED",
        "message": (
            f"The question's pieces don't connect: "
            f"{shown(subjects, [*config.measures, *config.metric_recipes])} can't reach "
            f"{shown(dimensions, config.dimensions)}{reached} ({error.get('code')}: "
            f"{error.get('message')}). Plan offers no draft."
        ),
        "details": {
            "subjects": subjects,
            "dimensions": dimensions,
            **({"entity": target} if target else {}),
            "error": {"code": error.get("code"), "message": error.get("message")},
        },
        "recovery_hints": [
            {
                "kind": "use_reachable_dimension",
                "message": (
                    "Group or filter by a dimension the measure reaches, or pick a measure that "
                    "reaches the dimension (discover lists what each reaches), then plan again; "
                    "or ask the user."
                ),
            }
        ],
    }


__all__ = ["PLACEHOLDER", "unmatched_subject_why", "unconnected_why", "unnamed_subject_terms"]
