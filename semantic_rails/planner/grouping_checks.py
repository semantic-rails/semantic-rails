"""Readiness: groupings the question asks for that the draft dropped."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from ..errors import SemanticLayerError
from ._base import _NAME_CONNECTORS, _last_token, _object_by_id
from .generators import _grouping_term_matches
from .groupings import (
    _grouping_matches,
    _is_temporal_grouping_term,
    _listed_grouping_terms,
    _name_forms,
    _strip_leading_rank_count,
    _term_matches_value_domain,
)
from .plan_query import _validate_query, _where_filters
from .time_phrases import _names_time_axis
from .time_windows import _time_window
from .visibility import visible_dimensions, visible_object_ids, visible_value_domains


def _names_whole_entity(term: str, entity: Any) -> bool:
    """Whether a grouping term names an entity by every word of its label: "customer" names
    Customer, but not Customer history or Customer segment membership."""

    content = set(re.findall(r"[^\W_]+", term.lower())) - _NAME_CONNECTORS
    label = str(entity.label or _last_token(entity.name)).lower()
    return _grouping_matches(term, entity, entity=True) and all(
        _name_forms({word}) & content
        for word in set(re.findall(r"[^\W_]+", label)) - _NAME_CONNECTORS
    )


def _named_grouping_spans(text: str, config: Any) -> list[tuple[int, int]]:
    """Grouping clauses read only by the dropped-grouping guard, with source spans.

    Keep this separate from planning and from the unasked-grouping guard: recognizing
    another obligation may hold a draft, but must never authorize one previously held.
    """

    lowered = str(text or "").lower()
    for start, end in _time_window(text).spans:
        lowered = lowered[:start] + "," + " " * (end - start - 1) + lowered[end:]

    def named(term: str) -> bool:
        return (
            _is_temporal_grouping_term(term)
            or any(_names_time_axis(term, row.label) for row in config.temporal_roles)
            or any(row.calendar_id and _names_time_axis(term, row.label) for row in config.entities)
            or any(_grouping_matches(term, row) for row in config.dimensions)
            or any(_grouping_matches(term, row, entity=True) for row in config.entities)
        )

    spans: list[tuple[int, int]] = []
    # A grouping marker inside a declared value name ("Sends per account")
    # belongs to that name, not to an additional grouping clause.
    value_spans = [
        match.span()
        for row in [*config.measures, *config.metric_recipes]
        for name in [row.label, _last_token(row.name), *(row.aliases or [])]
        if (words := re.findall(r"[^\W_]+", name.lower()))
        for match in re.finditer(r"\b" + r"\s+".join(map(re.escape, words)) + r"\b", lowered)
    ]
    # Rankings put the grouping before "by"; ordinary clauses put it after their
    # introducer. Whitespace and commas don't erase the obligation.
    clauses = re.finditer(
        r"^\s*(?:the\s+)?(?:top|highest|lowest)\s+(?P<ranked>[a-z0-9\s_,&-]+?)\s+by\b"
        r"|\b(?:by|per|(?:for\s+)?each|every)(?:\s+|\s*,\s*)(?P<listed>[a-z0-9\s_,&-]+?)"
        r"(?=\s+(?:by|per|each|every|at|where|for|from|in|with|during|over|having|who|that|"
        r"last|this|current|next|prior|sorted)\b|[.?!;]|$)",
        lowered,
    )
    for match in clauses:
        group = "ranked" if match.group("ranked") is not None else "listed"
        start, end = match.span(group)
        if group == "listed" and any(
            low <= match.start() and end <= high for low, high in value_spans
        ):
            continue
        if group == "ranked":
            raw = match.group(group)
            stripped = _strip_leading_rank_count(raw)
            start += raw.find(stripped)
        cursor = start
        after_comma = False
        for separator in [
            *re.finditer(r",\s*(?:and\b)?|\band\b|&", lowered[start:end]),
            None,
        ]:
            stop = start + separator.start() if separator else end
            piece = lowered[cursor:stop]
            low = cursor + len(piece) - len(piece.lstrip())
            high = cursor + len(piece.rstrip())
            if low < high and set(re.findall(r"[^\W_]+", lowered[low:high])) - _NAME_CONNECTORS:
                if after_comma and not named(lowered[low:high]):
                    break
                spans.append((low, high))
            if separator:
                cursor = start + separator.end()
                after_comma = "," in separator.group()
    return sorted(set(spans))


def _named_grouping_terms(text: str, config: Any) -> list[str]:
    """Add obligations to the legacy list without removing any of its holds."""

    terms = _listed_grouping_terms(text, config)
    legacy = {" ".join(item.split()) for item in terms}
    lowered = str(text or "").lower()
    for start, end in _named_grouping_spans(text, config):
        term = " ".join(lowered[start:end].split())
        if term not in legacy:
            terms.append(term)
    return terms


def _entity_grouping_dimensions(config: Any, term: str) -> set[str] | None:
    """The dimensions that may stand for a listed grouping naming an entity, or None when the
    term names no entity.

    Only an entity the term names by its whole label, with a one-column key, has any: its key
    dimension, and its one declared dimension whose own words name the term when no other
    does. A clock the entity declares is not one of them. Another entity's dimension never
    stands in, and a composite key has none, so the grouping stays unmatched.
    """

    entities = [row for row in config.entities if _grouping_matches(term, row, entity=True)]
    if not entities:
        return None
    clocks = {row.dimension for row in config.temporal_roles}
    allowed: set[str] = set()
    for entity in entities:
        if len(entity.key) != 1 or not _names_whole_entity(term, entity):
            continue
        owned = [row for row in config.dimensions if row.entity == entity.id]
        allowed |= {row.id for row in owned if row.column == entity.key[0]}
        named = [
            row.id
            for row in owned
            if row.column != entity.key[0] and row.id not in clocks and _grouping_matches(term, row)
        ]
        if len(named) == 1:
            allowed |= set(named)
    return allowed


def _query_clocks(config: Any, query: dict[str, Any]) -> list[str]:
    """The labels of the time block's clock: its temporal role, and the calendar it buckets on."""

    time = _time_of(query)
    return [
        str(row.label or "")
        for row in [
            _object_by_id(config.temporal_roles, str(time.get("temporal_role") or "")),
            *(
                row
                for row in config.entities
                if row.calendar_id and row.calendar_id == time.get("calendar_id")
            ),
        ]
        if row is not None
    ]


def _listed_dimension_terms(config: Any, question: str, query: dict[str, Any]) -> list[str]:
    """The listed groupings a dimension answers: not a clock term ("by month", "by order
    date"), which is the time block's, nor a declared value, which is a filter."""

    clocks = _query_clocks(config, query)
    return [
        term
        for term in _named_grouping_terms(question, config)
        if not (
            _is_temporal_grouping_term(term)
            or any(_names_time_axis(term, clock) for clock in clocks)
            or _term_matches_value_domain(config, term)
        )
    ]


def _reads_grouping(term: str, ids: set[str] | None, row: Any) -> bool:
    """Whether a dimension is a reading of a listed grouping: one of the entity's stand-ins
    (``_entity_grouping_dimensions``) for a term naming an entity, else a dimension whose own
    words name the term."""

    return _grouping_matches(term, row) if ids is None else row.id in ids


def _time_of(query: dict[str, Any]) -> dict[str, Any]:
    raw = (query or {}).get("time")
    return raw if isinstance(raw, dict) else {}


# A word asking for the rows at the level of the groupings it follows ("store name levels").
_LEVEL_WORD_RE = re.compile(r"\b(?:levels?|grains?)\b")


def _declared_name_spans(
    config: Any, lowered: str, *, underscores: bool = False
) -> dict[tuple[int, int], list[Any]]:
    """Where the question names a declared dimension, measure, metric recipe or entity, with
    the objects each span names, as ``(kind, row)``.

    A name is a label (also without its parenthetical: "item revenue" for Item revenue
    (USD)), the last part of the object's name or an alias, matched as whole words. A span
    inside a longer one is part of that name: "customer type" names Customer type, not the
    entity Customer as well. ``underscores`` also joins words with underscores and keeps
    declared boundary underscores ("_new_type").
    """

    groups: tuple[tuple[str, list[Any]], ...] = (
        ("dimension", config.dimensions),
        ("value", [*config.measures, *config.metric_recipes]),
        ("entity", config.entities),
    )
    found: dict[tuple[int, int], list[Any]] = {}
    for kind, rows in groups:
        for row in rows:
            label = str(row.label or "")
            names = {label, re.sub(r"\s*\(.*?\)", "", label), _last_token(row.name)}
            for name in names | set(row.aliases or []):
                spelling = str(name).lower()
                words = re.findall(r"[^\W_]+", spelling)
                if not words:
                    continue
                patterns = {r"\b" + r"\s+".join(map(re.escape, words)) + r"\b"}
                if underscores:
                    patterns = {
                        r"(?<![^\W_])" + body + r"(?![^\W_])"
                        for body in (re.escape(spelling), r"[\s_]+".join(map(re.escape, words)))
                    }
                for pattern in patterns:
                    for match in re.finditer(pattern, lowered):
                        found.setdefault(match.span(), []).append((kind, row))
    return {
        (low, high): named
        for (low, high), named in found.items()
        if not any(a <= low and high <= b and b - a > high - low for a, b in found)
    }


def _clock_spans(config: Any, lowered: str, query: dict[str, Any]) -> list[tuple[int, int]]:
    """Where the question names a clock, the time block's: "week", or "order date" for Order
    time. A clock phrase is words joined by spaces only, never with a level word."""

    clocks = _query_clocks(config, query)
    words = list(re.finditer(r"[^\W_]+", lowered))
    spans: list[tuple[int, int]] = []
    for index, first in enumerate(words):
        for last in words[index:]:
            text = lowered[first.start() : last.end()]
            if _LEVEL_WORD_RE.search(text) or not re.fullmatch(r"[^\W_]+(?:\s+[^\W_]+)*", text):
                break
            if _is_temporal_grouping_term(text) or any(_names_time_axis(text, c) for c in clocks):
                spans.append((first.start(), last.end()))
    return spans


def _pinned_fields(query: dict[str, Any]) -> set[str]:
    """The fields the draft's ``where`` pins to one value (``=``, or ``IN`` with one value)."""

    pinned: set[str] = set()
    for row in _where_filters(query):
        op, value = str(row.get("op")).lower(), row.get("value")
        if "field" in row and (
            (op == "=" and not isinstance(value, (list, tuple, dict, type(None))))
            or (op == "in" and isinstance(value, list) and len(value) == 1)
        ):
            pinned.add(str(row["field"]))
    return pinned


def _level_groupings_unmet(config: Any, question: str, query: dict[str, Any]) -> list[str]:
    """The groupings a question asking for a level or grain names that the draft doesn't
    group by.

    It reads which declared names the question holds (``_declared_name_spans``), never how
    the phrase around them is built, so a plural, a repeated "at" or a separator changes
    nothing. It runs only when "level", "levels", "grain" or "grains" stands outside every
    declared name (a measure named Stock level triggers nothing). Then every dimension or
    entity the question names must be grouped: a dimension by its own id, an entity by one of
    its stand-ins (``_entity_grouping_dimensions``). A declared value, a dimension the draft's
    ``where`` pins to one value (``_pinned_fields``), and a name inside a clock phrase
    (``_clock_spans``) need nothing. The word before each level word, past commas and
    connectors, must end the name of a dimension, an entity or a clock; any other word
    ("region level" with no Region) is unmet as well. The check only holds a plan.
    """

    lowered = str(question or "").lower()
    if not _LEVEL_WORD_RE.search(lowered):
        return []
    spans = _declared_name_spans(config, lowered)
    triggers = [
        match
        for match in _LEVEL_WORD_RE.finditer(lowered)
        if not any(low <= match.start() and match.end() <= high for low, high in spans)
    ]
    if not triggers:
        return []
    clock_spans = _clock_spans(config, lowered, query)
    pinned = _pinned_fields(query)
    grouped = set(query.get("group_by") or [])
    unmet: list[str] = []
    for (low, high), named in sorted(spans.items()):
        term = " ".join(lowered[low:high].split())
        dimensions = {row.id for kind, row in named if kind == "dimension"}
        entity = any(kind == "entity" for kind, _ in named)
        if (
            not (dimensions or entity)
            or any(a <= low and high <= b for a, b in clock_spans)
            or _term_matches_value_domain(config, term)
        ):
            continue
        stand_ins = (_entity_grouping_dimensions(config, term) or set()) if entity else set()
        if not (dimensions & (grouped | pinned) or stand_ins & grouped):
            unmet.append(term)
    ends = {high for (_, high), named in spans.items() if any(kind != "value" for kind, _ in named)}
    ends |= {high for _, high in clock_spans}
    words = list(re.finditer(r"[^\W_]+", lowered))
    for trigger in triggers:
        before = [
            word
            for word in words
            if word.end() <= trigger.start() and word.group() not in _NAME_CONNECTORS
        ]
        if not before:
            unmet.append(trigger.group())
        elif before[-1].end() not in ends:
            unmet.append(before[-1].group())
    return list(dict.fromkeys(unmet))


def _grouping_filter_value_spans(
    config: Any, lowered: str, query: dict[str, Any]
) -> list[tuple[tuple[int, int], str, str]]:
    """Source spans of declared values carried by a positive draft filter, with the filter's
    field and the value, matching the literal, label and alias spellings value inference does.
    """

    filters = _where_filters(query)
    spans: list[tuple[tuple[int, int], str, str]] = []
    for domain in config.value_domains:
        for value in domain.values or []:
            fields = [
                str(row["field"])
                for row in filters
                if row.get("field") in domain.dimensions
                and (
                    (row.get("op") == "=" and row.get("value") == value.value)
                    or (
                        str(row.get("op")).lower() == "in"
                        and isinstance(row.get("value"), list)
                        and value.value in row["value"]
                    )
                )
            ]
            for name in [value.value, value.label, *(value.aliases or [])]:
                phrase = str(name or "").strip().lower()
                if phrase and fields:
                    pattern = rf"(?<![a-z0-9]){re.escape(phrase)}s?(?![a-z0-9])"
                    spans.extend(
                        (match.span(), field, str(value.value))
                        for match in re.finditer(pattern, lowered)
                        for field in dict.fromkeys(fields)
                    )
    return spans


def _named_groupings_unmet(
    config: Any, question: str, query: dict[str, Any]
) -> tuple[list[str], list[dict[str, str]]]:
    """The caller-visible names in any question, read with spaces, underscores and any case
    (``_declared_name_spans``), that the draft leaves unmet, and the filters read from inside
    them as ``{"term", "field", "value"}``. Outside a clock phrase (``_clock_spans``), a
    dimension name needs its dimension grouped or pinned (``_pinned_fields``) by ``where``;
    an entity name needs a stand-in grouped in a level or grain
    question (elsewhere it often describes the measure: "repeat-customer orders"). No value word
    inside a name or word of the measure's label discharges it, and a positive filter whose value
    is read from inside a name holds the plan even when grouped. It never changes a draft.
    """

    # Every read below goes through this caller-scoped view: a hidden object is absent.
    kinds = ("dimensions", "entities", "measures", "metric_recipes", "temporal_roles")
    ids = set(visible_object_ids(config, (row.id for k in kinds for row in getattr(config, k))))
    rows = {k: [row for row in getattr(config, k) if row.id in ids] for k in kinds}
    scoped = replace(config, value_domains=visible_value_domains(config), **rows)
    lowered = str(question or "").lower()
    clock_spans = _clock_spans(scoped, lowered, query)
    values = _grouping_filter_value_spans(scoped, lowered, query)
    grouped = set(query.get("group_by") or [])
    settled = grouped | _pinned_fields({"where": query.get("where") or []})
    spans = _declared_name_spans(scoped, lowered, underscores=True)
    words = _LEVEL_WORD_RE.finditer(lowered)
    level = any(not any(a <= word.start() < b for a, b in spans) for word in words)
    unmet: list[str] = []
    inside: list[dict[str, str]] = []
    for (low, high), named in sorted(spans.items()):
        dimensions = {row.id for kind, row in named if kind == "dimension"}
        entity = any(kind == "entity" for kind, _ in named)
        if not (dimensions or entity) or any(a <= low and high <= b for a, b in clock_spans):
            continue
        term = " ".join(lowered[low:high].split())
        found = {(f, v): None for (start, end), f, v in values if low <= start and end <= high}
        for field, value in found:
            if (row := {"term": term, "field": field, "value": value}) not in inside:
                inside.append(row)
        stand_ins = (_entity_grouping_dimensions(scoped, term) or set()) if entity else set()
        if found or ((dimensions or level) and not (dimensions & settled or stand_ins & grouped)):
            unmet.append(term)
    return list(dict.fromkeys(unmet)), inside


def _dropped_grouping_why(
    runtime: Any,
    question: str,
    query: dict[str, Any],
    partial_query: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """A listed grouping that names an entity is satisfied only by that entity's own key
    dimension, or by the single declared dimension of that entity whose own words name it.
    An entity with a composite key is never satisfied by the guard, so the plan is not ready.

    Any other listed grouping needs a dimension whose own words name it; a clock term ("by
    month", "by order date") is the time block's and a declared value is a filter, so neither
    needs one. One dimension satisfies one listed grouping. A question asking for a level or
    grain must also have every grouping it names (``_level_groupings_unmet``), and any question
    every caller-visible dimension it names (``_named_groupings_unmet``), never a filter read from
    inside a name: ``details.filter_inside_grouping`` names each for the caller to confirm or remove.

    A grouping whose dimensions belong to two or more entities, none of them the measure's own
    ("name" for an order count: Customer name, Store name and more), is ambiguous: plan holds
    instead of picking one. Only the caller's ``partial_query`` group_by settles it, when the
    draft adds no other dimension that may be it.
    """

    from ..metadata import _selection_context  # noqa: WPS433 - shared metadata helper

    config = runtime._config
    try:
        root = _selection_context(config, query)["root_entity"]
    except SemanticLayerError:
        root = ""
    terms = _listed_dimension_terms(config, question, query)
    grouped = [
        row
        for item in dict.fromkeys(query.get("group_by") or [])
        if (row := _object_by_id(config.dimensions, item)) is not None
    ]
    visible = visible_dimensions(config)
    stand_ins = [
        _entity_grouping_dimensions(replace(config, dimensions=visible), term) for term in terms
    ]

    def reads(term: str, ids: set[str] | None, row: Any) -> bool:
        return _grouping_matches(term, row) if ids is None else row.id in ids

    chosen = set((partial_query or {}).get("group_by") or [])

    matches: list[list[Any]] = []
    for term, ids in zip(terms, stand_ins, strict=True):
        named = [row for row in visible if row.groupable and reads(term, ids, row)]
        if ids is None and not any(
            term.lower().replace("_", " ")
            in {
                str(row.label or "").lower().replace("_", " "),
                row.id.removeprefix("dimension.").lower().replace("_", " "),
            }
            for row in named
        ):
            # Discovery recognizes additional words/plurals. They may establish an
            # ambiguity unless the term names a whole strict label/ID. They never
            # satisfy a grouping the strict guard cannot read; columns don't settle it.
            discovered = set(_grouping_term_matches(runtime, query, term, limit=len(visible)) or [])
            matched_ids = {row.id for row in named} | discovered
            named = [row for row in visible if row.groupable and row.id in matched_ids]
        matches.append(named)

    def unsettled(term: str, ids: set[str] | None, named: list[Any]) -> bool:
        """Dimensions of two or more entities, none the measure's own, may be the grouping, and
        the caller's group_by doesn't say which: it names none, or the draft added one."""

        entities = {row.entity for row in named}
        picked = {row.id for row in grouped if reads(term, ids, row)}
        return len(entities) > 1 and root not in entities and not (picked and picked <= chosen)

    ambiguous = [
        term
        for term, ids, named in zip(terms, stand_ins, matches, strict=True)
        if unsettled(term, ids, named)
    ]
    candidates = [
        [
            index
            for index, dimension in enumerate(grouped)
            if term not in ambiguous
            and reads(term, ids, dimension)
            and (not any(row.entity == root for row in named) or dimension.entity == root)
        ]
        for term, ids, named in zip(terms, stand_ins, matches, strict=True)
    ]
    assigned: dict[int, int] = {}

    def assign(term_index: int, seen: set[int]) -> bool:
        for dimension_index in candidates[term_index]:
            if dimension_index in seen:
                continue
            seen.add(dimension_index)
            if dimension_index not in assigned or assign(assigned[dimension_index], seen):
                assigned[dimension_index] = term_index
                return True
        return False

    dropped = [term for index, term in enumerate(terms) if not assign(index, set())]
    named, inside = _named_groupings_unmet(config, question, query)
    held = list(dict.fromkeys(row["term"] for row in inside))
    dropped += [
        term
        for term in dict.fromkeys([*_level_groupings_unmet(config, question, query), *named])
        if term not in dropped
    ]
    if not dropped:
        return None
    unclear = [term for term in dropped if term in ambiguous]
    missing = [term for term in dropped if term not in ambiguous]
    lost = [term for term in missing if term not in held]
    # An option removes every draft grouping its term matches. When that could remove a
    # grouping another term needs, options would overwrite each other, so offer none.
    removals = [
        {row.id for row in named} & set(query.get("group_by") or [])
        for term, named in zip(terms, matches, strict=True)
        if term in unclear
    ]
    settled = {grouped[index].id for index in assigned}
    overlap = any(ids & settled for ids in removals) or any(
        first & second for index, first in enumerate(removals) for second in removals[index + 1 :]
    )
    clarification: dict[str, Any] = {}
    if unclear and not overlap:
        options = []
        for term, named in zip(terms, matches, strict=True):
            if term not in unclear:
                continue
            matching_ids = {row.id for row in named}
            for row in sorted(named, key=lambda row: row.id):
                patch = {
                    "group_by": [
                        item for item in query.get("group_by", []) if item not in matching_ids
                    ]
                    + [row.id],
                    "where": query.get("where", []),
                    "order_by": [
                        {**item, "field": row.id} if item.get("field") in matching_ids else item
                        for item in query.get("order_by", [])
                    ],
                }
                if _validate_query(runtime, {**query, **patch}, partial_query).get("ok"):
                    options.append(
                        {
                            "id": row.id,
                            "label": row.label,
                            "term": term,
                            **(
                                patch
                                if len(unclear) == 1
                                else {
                                    "replaces": [
                                        item
                                        for item in query.get("group_by", [])
                                        if item in matching_ids
                                    ]
                                }
                            ),
                        }
                    )
        clarification = {
            "clarification": {
                "question": "Which dimension does each ambiguous grouping mean?",
                "options": options,
            }
        }
    multiple_recovery = (
        " In best.query_ir, for each ambiguous term remove its option's replaces ids from "
        "group_by and their entries from order_by, add the chosen id to group_by, keep group_by ids sorted, "
        "then validate."
    )
    messages = [
        *(
            [
                f"The draft drops the grouping by {', '.join(lost)} that the question asks "
                "for: each listed grouping needs its own matching dimension, so plan doesn't "
                "call it ready."
            ]
            if lost
            else []
        ),
        *(
            [
                "The draft filters on a value read from inside a grouping name the question "
                "asks for ("
                + "; ".join(
                    f"{row['value']!r} of {row['field']} inside {row['term']!r}" for row in inside
                )
                + "): the question may name only the grouping, and best.query_ir as is "
                "returns only the filtered rows, so plan doesn't call it ready, even when the "
                "draft groups by that name."
            ]
            if inside
            else []
        ),
        *(
            [
                f"The grouping by {', '.join(unclear)} may be a dimension of any of several "
                "entities, none of them the measure's own, so plan doesn't pick one or call the "
                "draft ready." + (multiple_recovery if len(unclear) > 1 and not overlap else "")
            ]
            if unclear
            else []
        ),
    ]
    return {
        "code": "PLAN_UNMATCHED_TERMS",
        "message": " ".join(messages),
        "details": {
            "terms": dropped,
            "dropped_groupings": missing,
            **({"ambiguous_groupings": unclear} if unclear else {}),
            **({"filter_inside_grouping": inside} if inside else {}),
            **clarification,
        },
        "recovery_hints": [
            {
                "kind": "clarify_grouping" if unclear else "use_named_objects",
                "message": (
                    (
                        "Find a dimension for each grouping with discover, add the missing ones to "
                        "best.query_ir group_by, then validate; or ask the user which grouping they "
                        "mean."
                        if lost or unclear
                        else ""
                    )
                    + (
                        " Two groupings could replace the same draft dimension, so plan offers "
                        "no options: ask the user which dimension each grouping the question "
                        "lists means, then make exactly those ids best.query_ir group_by and "
                        "validate."
                        if overlap
                        else multiple_recovery
                        if len(unclear) > 1
                        else " Apply an option's group_by, where and order_by to best.query_ir, then validate."
                        if unclear
                        else ""
                    )
                    + (
                        " For each details.filter_inside_grouping entry, confirm with the user that "
                        "the question asks for that value, or remove the value from that field's "
                        "filters in best.query_ir where; make sure group_by has the grouping the "
                        "term names, then validate."
                        if inside
                        else ""
                    )
                ).strip(),
            }
        ],
    }
