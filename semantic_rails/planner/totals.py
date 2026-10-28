"""A hint, on a held plan, for the total its question asks for.

plan never drafts a total: its status and ``best.query`` are the question's own. When the
question asks for a grand total, subtotals, each group's total or each group's share, and its
plan isn't ``ok``, the plan carries a ``PLAN_TOTALS_HINT`` warning (severity ``info``) with the
call shape that adds it, named with the ids of ``best.query``:

- "with the grand total", "with a total row": ``{"totals": []}``;
- "with subtotals by store", "with totals by store": ``{"totals": [<the store's columns>]}``;
- "with each store's total (across months)", "the total for each store": a ``total`` item
  over the call's other columns (and the period);
- "each store's share of revenue": a ``share`` item over the named columns.

A catalog name holding a total's word ("Order total", "Food revenue share"), one value as a
part of another ("food revenue as a share of total revenue"), and a share the package declares
get no hint: they are the package's own objects, or not a total at all.
"""

from __future__ import annotations

import re
from typing import Any

from ..query_call import TIME_AXIS
from ._base import _singular, _tokens
from .call import Call, Select
from .groupings import _entity_stand_ins

_APOSTROPHE = "['’]"
_END = r"(?=\s*(?:$|[,.;:!?)]|\s+(?:and|with|plus)\b))"
# Groupings a phrase lists: "store", "customer type", "types and months", "store, channel".
_ITEM = r"[a-z]+(?:\s+(?!(?:and|with|plus)\b)[a-z]+)?"
_LIST = rf"{_ITEM}(?:(?:\s*,\s*|\s+and\s+)(?!(?:the|an?|each|every|with|plus|its|their)\b){_ITEM})*"
# A total row: "with the grand total", "plus a total row", "and the overall total". "Total" asks
# for a row only where the clause ends after it: in "total revenue" it is the measure's own word.
_ROWS = re.compile(
    r"\b(?:with|plus|including|and)\s+(?:an?\s+|the\s+)?"
    r"(?:(?:grand|overall)\s+totals?|totals?)(?:\s+rows?|\s+line)?"
    r"(?:\s+at\s+the\s+(?:bottom|end))?" + _END + r"|\bgrand\s+totals?\b",
    re.IGNORECASE,
)
# Subtotals: "with subtotals by store", or "with totals by store" (plural: "revenue total by
# store" is revenue by store).
_SUBTOTALS = re.compile(
    r"\b(?:(?:with|and|plus|including)\s+)?(?:the\s+)?sub-?totals?"
    rf"(?:\s+(?:by|per|for)\s+(?:each\s+|every\s+|the\s+)?(?P<words>{_LIST}))?" + _END + "|"
    r"\b(?:with|and|plus|including)\s+(?:the\s+)?totals\s+(?:by|per)\s+(?:each\s+|every\s+)?"
    rf"(?P<by>{_LIST})" + _END,
    re.IGNORECASE,
)
_BESIDE = re.compile(
    r"\b(?:(?:with|and|plus|including)\s+)?(?:each|every)\s+"
    rf"(?P<words>[a-z]+(?:\s+[a-z]+)?){_APOSTROPHE}s?\s+totals?"
    r"(?:\s+(?:across|over)\s+(?:all\s+)?(?:the\s+|its\s+|their\s+)?"
    rf"(?P<across>{_LIST}))?" + _END + "|"
    r"\b(?:(?:with|and|plus|including)\s+)?(?:the\s+)?totals?\s+(?:for|per|of)\s+"
    r"(?:each|every)\s+(?P<per>[a-z]+(?:\s+[a-z]+)?)" + _END,
    re.IGNORECASE,
)
_SHARE = re.compile(
    rf"\b(?:each|every)\s+(?P<words>[a-z]+(?:\s+[a-z]+)?){_APOSTROPHE}s?\s+share\b",
    re.IGNORECASE,
)
# Checked in this order: a share or a subtotal also reads "total", and "each store's total"
# would read as a total row otherwise.
_PHRASES = (("share", _SHARE), ("subtotals", _SUBTOTALS), ("beside", _BESIDE), ("rows", _ROWS))
# One value as a part of another: "as a share of", "share of Y that was X", any percent word.
_PART_OF = re.compile(
    r"\bas\s+(?:an?\s+)?(?:share|percent(?:age)?|proportion|fraction)\b|"
    r"\b(?:share|percent(?:age)?|proportion|fraction)\s+of\b.*?"
    r"\b(?:that|which|was|were|is|are|came|comes|come|from|within|among)\b|"
    r"\b(?:percent(?:age)?|proportion|fraction)\b",
    re.IGNORECASE,
)
_PRONOUNS = {"one", "it", "its", "their", "them"}
_TOTAL_WORDS = {"total", "subtotal", "share", "percent", "percentage", "proportion"}
_WHAT = {
    "rows": "a grand total row",
    "subtotals": "subtotal rows",
    "beside": "each group's total beside its rows",
    "share": "each group's share",
}
_VALUE, _GROUPING = "<measure or metric id>", "<group_by id>"


def with_totals_hint(config: Any, payload: dict[str, Any], question: str) -> dict[str, Any]:
    """``payload`` with ``PLAN_TOTALS_HINT`` among its warnings when it isn't ``ok`` and its
    question asks for a total; anything else is returned as it is."""
    if payload.get("status") == "ok":
        return payload
    asked = _asked(question, config)
    if asked is None:
        return payload
    kind, words, across, phrase = asked
    query = (payload.get("best") or {}).get("query") or {}
    hint = {
        "code": "PLAN_TOTALS_HINT",
        "severity": "info",
        "message": (
            f"The question asks for {_WHAT[kind]} ('{phrase}'), which plan doesn't draft. Once "
            "best.query answers the rest of the question, add details.add to it (named with "
            "best.query's ids) and validate it before executing."
        ),
        "details": {
            "kind": kind,
            "phrase": phrase,
            "add": _shape(config, query, kind, words, across),
        },
    }
    return {**payload, "warnings": [*payload.get("warnings", []), hint]}


def _asked(question: str, config: Any) -> tuple[str, str, str, str] | None:
    """The total phrase a question holds, as (kind, its grouping words, what a group's total
    adds up across, the phrase), or None."""
    singular = [_singular(token) for token in _tokens(question)]
    spoken = f" {' '.join(singular)} "
    if _PART_OF.search(question) or any(f" {name} " in spoken for name in _catalog_names(config)):
        return None
    for kind, pattern in _PHRASES:
        match = pattern.search(question)
        if match is None:
            continue
        if kind == "share" and _declared_share(config, set(singular)):
            return None
        groups = match.groupdict()
        words = (groups.get("words") or groups.get("by") or groups.get("per") or "").strip()
        words = "" if words.lower() in _PRONOUNS else words  # "each one's share"
        return kind, words, (groups.get("across") or "").strip(), match[0].strip()
    return None


def _catalog_names(config: Any) -> set[str]:
    """The labels, names and aliases of catalog objects that hold a total's word, as tokens."""
    rows = [*config.measures, *config.metric_recipes, *config.dimensions, *config.segments]
    names = (name for row in rows for name in (row.label, row.name, *row.aliases))
    phrases = (" ".join(_singular(token) for token in _tokens(name)) for name in names)
    return {phrase for phrase in phrases if set(phrase.split()) & _TOTAL_WORDS}


def _declared_share(config: Any, said: set[str]) -> bool:
    """Whether a ratio metric, or one named with a share's word, has every label word said."""
    for metric in config.metric_recipes:
        words = {_singular(token) for token in _tokens(metric.label or metric.name)}
        if words and words <= said and (metric.kind == "ratio" or words & _TOTAL_WORDS):
            return True
    return False


def _shape(
    config: Any, query: dict[str, Any], kind: str, words: str, across: str
) -> dict[str, Any]:
    """The call fields that add the total to ``query``, with its ids where it has them."""
    group_by = list(query.get("group_by") or [])
    grain = str((query.get("time") or {}).get("grain") or "")
    named = _named_list(config, group_by, words, grain) if words else []
    if kind == "rows":
        return {"totals": []}
    if kind == "subtotals":
        kept = [column for column in named if column != TIME_AXIS]
        kept = kept or _with_partners(config, group_by, group_by[:1])
        return {"totals": _levels(config, group_by, kept) or [_GROUPING]}
    if kind == "share":
        over = named or group_by or [_GROUPING]
    else:
        over = _named_list(config, group_by, across, grain) if across else []
        over = over or [column for column in group_by if column not in named]
        if grain and not across and TIME_AXIS not in named:
            over.append(TIME_AXIS)
        over = over or [_GROUPING]
    sources = [
        item if isinstance(item, str) else item.get("expression")
        for item in query.get("select") or []
    ]
    values = [
        source
        for source in sources
        if isinstance(source, str) and source.startswith(("measure.", "metric."))
    ] or [_VALUE]
    item_kind = "share" if kind == "share" else "total"
    items = (
        Select(
            {"kind": item_kind, "input": value, "over": over},
            f"{'value' if value == _VALUE else value.rsplit('.', 1)[-1]}_{item_kind}",
        )
        for value in values
    )
    return Call(select=tuple(items)).to_call()


def _named_list(config: Any, group_by: list[str], words: str, grain: str) -> list[str]:
    """The call's columns a list of groupings names ("types and months"), or none when an item
    names none of them."""
    named: set[str] = set()
    for item in re.split(r"\s*,\s*|\s+and\s+", words.strip()):
        columns = _named_columns(config, group_by, item, grain)
        if not columns:
            return []
        named.update(columns)
    return [column for column in [*group_by, TIME_AXIS] if column in named]


def _named_columns(config: Any, group_by: list[str], words: str, grain: str) -> list[str]:
    """The call's columns ``words`` name: ``period`` by the grain's word; an entity's key and
    name when the words are the entity's whole name; else a group_by dimension by its own
    words ("type" names "Customer type")."""
    wanted = _singulars(words)
    if not wanted:
        return []
    if grain and wanted <= {grain, "period"}:
        return [TIME_AXIS]
    entities = {
        entity.id
        for entity in config.entities
        if any(wanted == _singulars(name) for name in (entity.label, entity.name, *entity.aliases))
    }
    if entities:
        named = [
            column
            for entity, keys, shown in _stand_ins(config, group_by)
            if entity.id in entities
            for column in (*keys, *shown)
        ]
        return _with_partners(config, group_by, named)
    dimensions = {row.id: row for row in config.dimensions}
    named = [
        column
        for column in group_by
        if column in dimensions
        and wanted <= _singulars(f"{dimensions[column].label} {dimensions[column].name}")
    ]
    return _with_partners(config, group_by, named)


def _singulars(text: str) -> set[str]:
    return {_singular(token) for token in _tokens(text)}


def _stand_ins(config: Any, group_by: list[str]) -> list[tuple[Any, list[str], list[str]]]:
    """Each entity whose rows the call's columns stand for, with the key and name columns of
    it the call groups by."""
    out = []
    for entity in config.entities:
        keys, shown = _entity_stand_ins(config, entity, str(entity.label or ""))
        keys, shown = [c for c in keys if c in group_by], [c for c in shown if c in group_by]
        if keys or shown:
            out.append((entity, keys, shown))
    return out


def _with_partners(config: Any, group_by: list[str], columns: list[str]) -> list[str]:
    """``columns`` with the key or name grouped beside each, in group_by order."""
    chosen = set(columns)
    for _entity, keys, shown in _stand_ins(config, group_by):
        if chosen & {*keys, *shown}:
            chosen |= {*keys, *shown}
    return [column for column in group_by if column in chosen]


def _levels(config: Any, group_by: list[str], kept: list[str]) -> list[str]:
    """The ``totals`` that keep ``kept``: an entity's name without its key, since a level keeps
    the key of each name it keeps."""
    keys = {
        key
        for _entity, entity_keys, shown in _stand_ins(config, group_by)
        if shown and set(shown) <= set(kept)
        for key in entity_keys
    }
    return [column for column in kept if column not in keys]
