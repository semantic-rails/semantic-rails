"""The groupings and time grain a question asks for."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from ._base import (
    _NAME_CONNECTORS,
    _dimension,
    _last_token,
    _object_by_id,
    _requested_grouping_spans,
    _runtime_composition_terms,
    _singular,
    _strip_leading_rank_count,
    _tokens,
)
from .time_phrases import _TIME_UNITS, _names_time_axis
from .time_windows import _time_window
from .visibility import visible_dimensions, visible_value_domains


def _explicit_grain(text: str, clock: str = "") -> str:
    """Return the grain the intent explicitly cues ("" when absent).

    With ``clock``, the label of the query's temporal role, a grouping that names that clock
    ("by order month", "by order date") takes its own unit, else a cadence the question
    names ("monthly", "per week", "at week grain"), else days. A unit that only a window
    names ("for the first quarter of 2017", "last month") doesn't set it.
    """
    lowered = str(text or "").lower()
    terms = _runtime_composition_terms(text)
    for term in _requested_grouping_terms(text) if clock else ():
        if _names_time_axis(term, clock):
            tokens = set(_tokens(term))
            cues = ("by {}", "per {}", "each {}", "{} grain", "{}ly")
            own = [unit for unit in _TIME_UNITS if unit in tokens]
            cadence = [
                unit
                for unit in _TIME_UNITS
                if any(cue.format(unit) in lowered for cue in cues)
                or (unit == "day" and "daily" in lowered)
            ]
            return (own or cadence or ["day"])[0]
    for candidate in _TIME_UNITS:
        if (
            f"by {candidate}" in lowered
            or f"per {candidate}" in lowered
            or f"each {candidate}" in lowered
            or f"{candidate}ly" in lowered
            or candidate in terms
        ):
            return candidate
    return ""


def _single_bucket_grain(bounds: dict[str, Any]) -> str:
    """The finest calendar grain that holds the whole window in one bucket, or ``""``."""

    try:
        start = date.fromisoformat(str(bounds["start"])[:10])
        last = date.fromisoformat(str(bounds["end"])[:10]) - timedelta(days=1)
    except (KeyError, ValueError):
        return ""
    if last < start:
        return ""
    if start.year != last.year:
        # Whole calendar years ("in 2016 and 2017") read as one total per year.
        whole_years = (start.month, start.day, last.month, last.day) == (1, 1, 12, 31)
        return "year" if whole_years else ""
    if start == last:
        return "day"
    if start.weekday() == 0 and last == start + timedelta(days=6):
        return "week"
    if start.month == last.month:
        return "month"
    if (start.month - 1) // 3 == (last.month - 1) // 3:
        return "quarter"
    return "year"


def _time_spec(role: str, text: str, clock: str = "") -> dict[str, Any]:
    lowered = str(text or "").lower()
    grain = _explicit_grain(text, clock)
    window = _time_window(text)
    if not grain and window.relative_unit:
        # A relative window ("last 7 days", "yesterday") buckets at its own
        # unit instead of the generic month default.
        grain = window.relative_unit
    if not grain and not _TREND_CUE_RE.search(lowered):
        # A total over a calendar window ("revenue in 2017", "orders in the
        # first half of 2017") needs a grain that yields one bucket.
        grain = _single_bucket_grain(window.bounds)
    return {"temporal_role": role, "grain": grain or "month", **window.bounds}


# Cues that ask for a series over time even without a named grain.
_TREND_CUE_RE = re.compile(r"\b(?:over time|trends?|trending|history|historical|time series)\b")


def _name_forms(words: Iterable[str]) -> set[str]:
    """The words with their regular plurals, which name the same object; synonyms do not."""

    words = set(words)
    return (
        words
        | {word + "s" for word in words}
        | {word[:-1] + "ies" for word in words if word.endswith("y")}
        | {
            word + "es"
            for word in words
            if len(word) > 2 and word.endswith(("s", "x", "z", "ch", "sh"))
        }
    )


def _grouping_matches(term: str, row: Any, *, entity: bool = False) -> bool:
    """Match content words to declared names, never substring scores or descriptions.

    A dimension's own words are its label, its aliases and the last part of its name; the
    namespace, model and entity prefix in its id are not its words. An entity's are its name,
    its label and its synonyms.
    """

    names = [row.name if entity else _last_token(row.name), row.label, *(row.aliases or [])]
    words = _name_forms(re.findall(r"[^\W_]+", " ".join(names).lower()))
    content = set(re.findall(r"[^\W_]+", term.lower())) - _NAME_CONNECTORS
    return bool(content and content <= words)


def _names_whole_entity(term: str, entity: Any) -> bool:
    """Whether a grouping term names an entity by every word of its label or of one of its
    synonyms: "customer" names Customer, but not Customer history or Customer segment
    membership."""

    content = set(re.findall(r"[^\W_]+", term.lower())) - _NAME_CONNECTORS
    names = [str(entity.label or _last_token(entity.name)), *(entity.aliases or [])]
    return _grouping_matches(term, entity, entity=True) and any(
        (words := set(re.findall(r"[^\W_]+", str(name).lower())) - _NAME_CONNECTORS)
        and all(_name_forms({word}) & content for word in words)
        for name in names
    )


def _entity_stand_ins(config: Any, entity: Any, term: str = "") -> tuple[list[str], list[str]]:
    """The dimensions that stand for an entity's rows in an answer: its key dimensions (on its
    one-column key), and the one that names a row beside them.

    That is its ``display`` dimension, else its one dimension (not a clock) whose own words name
    ``term`` ("Store name" for "store"), else none. A composite key, or a key the caller can't
    see, has no stand-ins. Drafting and every readiness check read the entity through this.
    """

    if len(entity.key) != 1:
        return [], []
    owned = [row for row in visible_dimensions(config) if row.entity == entity.id]
    keys = [row.id for row in owned if row.column == entity.key[0]]
    others = [row for row in owned if row.column != entity.key[0]]
    shown = [row.id for row in others if entity.display and row.id == entity.display]
    if not shown:
        clocks = {row.dimension for row in config.temporal_roles}
        shown = [row.id for row in others if row.id not in clocks and _grouping_matches(term, row)]
    return keys, shown if len(shown) == 1 else []


def _entity_grouping(config: Any, term: str) -> tuple[Any, list[str]] | None:
    """The one entity a grouping term names by its whole label or a synonym (plurals allowed,
    never a description), with its stand-ins, key first; None when the term names no entity,
    or several, or the entity has no one key dimension the caller can see."""

    named = [row for row in config.entities if _names_whole_entity(term, row)]
    if len(named) != 1:
        return None
    keys, shown = _entity_stand_ins(config, named[0], term)
    return (named[0], [*keys, *shown]) if len(keys) == 1 else None


def _display_entities(config: Any, query: dict[str, Any]) -> list[Any]:
    """The entities with a visible ``display`` dimension that the draft's subject reaches: whom
    "who" may list when its clause names no entity."""

    from ..errors import SemanticLayerError  # noqa: WPS433
    from ..metadata import _availability_for_object, _selection_context  # noqa: WPS433

    out: list[Any] = []
    try:
        root = _selection_context(config, query)["root_entity"]
        for entity in config.entities:
            keys, shown = _entity_stand_ins(config, entity)
            if (
                entity.display
                and len(keys) == 1
                and shown == [entity.display]
                and _availability_for_object(config, root, entity.id, "entity")["available"]
            ):
                out.append(entity)
    except SemanticLayerError:
        return []
    return out


def _key_only_assumptions(
    config: Any, query: dict[str, Any], partial_query: dict[str, Any] | None = None
) -> list[str]:
    """A line for each entity the draft shows by its key alone because nothing else names its
    rows: the caller sees no display for it and no one dimension of its own names it. A
    caller's grouping needs none."""

    caller = set((partial_query or {}).get("group_by") or [])
    grouped = [
        row
        for item in dict.fromkeys(query.get("group_by") or [])
        if (row := _object_by_id(config.dimensions, item)) is not None
    ]
    lines: list[str] = []
    for row in grouped:
        entity = _object_by_id(config.entities, row.entity)
        if (
            entity is None
            or row.id in caller
            or _entity_stand_ins(config, entity, str(entity.label or "")) != ([row.id], [])
            or any(other.entity == row.entity for other in grouped if other is not row)
        ):
            continue
        lines.append(f"{entity.label} has no display name, so its rows show its key, {row.label}.")
    return lines


def _named_run(config: Any, lowered: str, start: int) -> tuple[int, int] | None:
    """The span of the longest run of up to four words from ``start`` that is a whole name of a
    visible dimension or entity (its label without a parenthetical, the last part of its name,
    or a synonym; singular or plural), or None. A grouping term ends at the first word that
    names nothing more: "each plan make" names "plan"."""

    words = list(re.finditer(r"[^\W_]+", lowered[start:]))[:4]
    names = {
        tuple(_singular(word) for word in re.findall(r"[^\W_]+", str(name).lower()))
        for row in [*visible_dimensions(config), *config.entities]
        for name in [
            re.sub(r"\s*\(.*?\)", "", str(row.label or "")),
            _last_token(row.name),
            *(row.aliases or []),
        ]
    }
    for size in range(len(words), 0, -1):
        low, high = start + words[0].start(), start + words[size - 1].end()
        said = tuple(_singular(word.group()) for word in words[:size])
        if said in names and re.fullmatch(r"[^\W_]+(?:\s+[^\W_]+)*", lowered[low:high]):
            return low, high
    return None


# "each" and "every" (also "for each") ask for a row per item of the name that follows.
_EACH_RE = re.compile(r"\b(?:each|every)\s+")


def _each_terms(config: Any, text: str) -> list[str]:
    """The names "each" or "every" ask for a row of ("How many orders did each store get")."""

    lowered = str(text or "").lower()
    spans = [_named_run(config, lowered, match.end()) for match in _EACH_RE.finditer(lowered)]
    return [lowered[span[0] : span[1]] for span in spans if span is not None]


def _requested_grouping_terms(text: str) -> list[str]:
    lowered = str(text or "").lower()
    return [lowered[start:end] for start, end in _requested_grouping_spans(text)]


def _listed_grouping_terms(text: str, config: Any) -> list[str]:
    """Every grouping the question lists, read only to decide whether a draft is ready.

    A draft reads its groupings with ``_requested_grouping_terms``, where a comma ends the
    list. Here the piece after a comma continues it when it names a clock, a dimension or an
    entity ("by incident name, incident" lists two). A window the question states is not part
    of a grouping's name: it separates pieces as a comma does, so the list goes on past it only
    with a piece that names one ("by store, last month" lists one; "by incident name, last
    month and incident" lists two). A listed grouping the draft lacks holds the plan, so
    reading more of the question can hold more plans but never makes one ready.
    """

    lowered = str(text or "").lower()
    match = re.search(
        r"^\s*(?:the\s+)?(?:top|bottom)\s+([a-z0-9 _,-]+?)\s+by\s+[a-z0-9 _-]+?(?:[.?!,;]|$)",
        lowered,
    )
    if match:
        raw_terms = _strip_leading_rank_count(match.group(1).strip())
    else:
        match = re.search(
            r"\bby ([a-z0-9 _,-]+?)(?:\s+(?:where|for|from|in|with|during|over|having|who|that)\b|[.?!;]|$)",
            lowered,
        )
        raw_terms = match.group(1).strip() if match else ""
    if not match or not raw_terms:
        return []
    # A recorded window is a clause boundary, read as a comma.
    offset = match.start(1) + match.group(1).find(raw_terms)
    for start, end in _time_window(text).spans:
        low, high = max(start, offset) - offset, min(end, offset + len(raw_terms)) - offset
        if low < high:
            raw_terms = raw_terms[:low] + "," + " " * (high - low - 1) + raw_terms[high:]
    pieces = re.split(r"(\s*(?:,\s*and |,| and | & | by )\s*)", raw_terms)
    terms: list[str] = []
    for index in range(0, len(pieces), 2):
        term = pieces[index].strip()
        if not term:
            continue
        if (
            index
            and "," in pieces[index - 1]
            and not (
                _is_temporal_grouping_term(term)
                or any(_names_time_axis(term, row.label) for row in config.temporal_roles)
                or any(
                    row.calendar_id and _names_time_axis(term, row.label) for row in config.entities
                )
                or any(_grouping_matches(term, row) for row in config.dimensions)
                or any(_grouping_matches(term, row, entity=True) for row in config.entities)
            )
        ):
            break
        terms.append(term)
    return terms


def _is_temporal_grouping_term(term: str) -> bool:
    term_tokens = set(_tokens(term))
    return bool(
        term in {"day", "week", "month", "quarter", "year", "delivered month", "ordered month"}
        or term_tokens
        and term_tokens.issubset(
            {"day", "week", "month", "quarter", "year", "time", "delivered", "ordered"}
        )
    )


def _term_matches_value_domain(config: Any, term: str) -> bool:
    term_tokens = set(_tokens(term))
    if not term_tokens:
        return False
    for domain in visible_value_domains(config):
        for row in list(domain.values or []):
            values = [
                str(row.value),
                str(row.label),
                *[str(alias) for alias in list(row.aliases or [])],
            ]
            for value in values:
                value_tokens = set(_tokens(value))
                if value_tokens and value_tokens.issubset(term_tokens):
                    return True
    return False


def _maybe_group_by(
    config: Any,
    text: str,
    *,
    target_terms: Iterable[str] = (),
    clock: str = "",
    each: bool = False,
) -> list[str]:
    """The dimensions the question's groupings ask for ("by", "top N … by", and with ``each``
    "each" or "every"). A term naming an entity is that entity's stand-ins
    (``_entity_grouping``), never a dimension scored by its description."""

    lowered = str(text or "").lower()
    terms = _runtime_composition_terms(text)
    target_set = {term for term in target_terms if term}
    group_by: list[str] = []
    if terms & {"segment", "segments"} and ("customer" in terms or "historical" in terms):
        dim = _object_by_id(visible_dimensions(config), "dimension.jaffle_customer_history_segment")
        if dim is not None:
            group_by.append(dim.id)
    if any(term in lowered for term in ("geo", "geography", "region", "parent")):
        dim = _dimension(config, ["geo"], prefer_parent="parent" in lowered)
        if dim is not None:
            group_by.append(dim.id)
    # A stated window ends the name a "by" term gives an entity ("by account last week"), as it
    # ends a listed grouping for readiness.
    windows = [start for start, _end in _time_window(text).spans]
    listed = [
        (lowered[low:high], lowered[low : min([high, *(at for at in windows if low < at)])], False)
        for low, high in _requested_grouping_spans(text)
    ]
    cued = [(term, term, True) for term in (_each_terms(config, text) if each else [])]
    for term, named, outright in [*listed, *cued]:
        term_tokens = set(_tokens(term))
        if (
            "geo" in term_tokens
            or _is_temporal_grouping_term(term)
            or _names_time_axis(term, clock)
            or _term_matches_value_domain(config, term)
        ):
            continue
        # "each" names a grouping outright; a "by" term may restate the measure.
        if target_set and term_tokens and not outright:
            metric_qualifier_words = {
                "count",
                "volume",
                "sum",
                "total",
                "value",
                "amount",
                "average",
                "mean",
            }
            content_tokens = term_tokens - metric_qualifier_words
            if content_tokens and content_tokens.issubset(target_set):
                continue
            if content_tokens and not (content_tokens - target_set):
                continue
        entity = _entity_grouping(config, named.strip())
        dim = _dimension(config, term_tokens) if entity is None else None
        group_by.extend(entity[1] if entity is not None else [dim.id] if dim is not None else [])
    return list(dict.fromkeys(group_by))
