"""A grand total, subtotals, each group's total, or a share of the total, asked in a question.

A question asking for one is planned without its total words (``asked_totals``), so they never
read as unmatched terms or as a metric; then the ready draft takes the call shape the engine
answers it with (``shaped_call``):

- "with the grand total", "with a total row": ``totals: []``, one total row (per period);
- "with subtotals by store": ``totals`` keeping the named columns, down to the grand total;
- "with each store's total", "the total for each store": a ``total`` item beside each row,
  over the call's other columns (and the period);
- "each store's share", "as a share of the total": a ``share`` item over the named columns
  (the call's columns when none is named).

``with_totals`` validates the shaped call as execute reads it, on the request's clock and
identity. When the engine refuses it (a policy that requires a grouping, withheld values, a
measure a share can't add up) or the draft has no place for it, the draft stays the question
without its total and the plan is held with ``PLAN_TOTALS_NOT_DRAFTED``: plan never hands back
a call execute refuses, nor a ready call that drops the total.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..clock import floor_period, period_end
from ..errors import SemanticLayerError
from ..query_call import TIME_AXIS
from ._base import _singular, _tokens
from .call import Call, QueryBuilder, Time
from .groupings import _entity_stand_ins
from .plan_query import _validate_query

_APOSTROPHE = "['’]"
_END = r"(?=\s*(?:$|[,.;:!?)]|\s+(?:and|with|plus)\b))"
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
    r"(?:\s+(?:by|per|for)\s+(?:each\s+|every\s+|the\s+)?(?P<words>[a-z][a-z ]*?))?" + _END + "|"
    r"\b(?:with|and|plus|including)\s+(?:the\s+)?totals\s+(?:by|per)\s+(?:each\s+|every\s+)?"
    r"(?P<by>[a-z][a-z ]*?)" + _END,
    re.IGNORECASE,
)
_BESIDE = re.compile(
    r"\b(?:(?:with|and|plus|including)\s+)?(?:each|every)\s+"
    rf"(?P<words>[a-z]+(?:\s+[a-z]+)?){_APOSTROPHE}s?\s+totals?"
    r"(?:\s+(?:across|over)\s+(?:all\s+)?(?:the\s+|its\s+|their\s+)?"
    r"(?P<across>[a-z]+(?:\s+(?!(?:and|with|plus)\b)[a-z]+)?))?" + _END + "|"
    r"\b(?:(?:with|and|plus|including)\s+)?(?:the\s+)?totals?\s+(?:for|per|of)\s+"
    r"(?:each|every)\s+(?P<per>[a-z]+(?:\s+[a-z]+)?)" + _END,
    re.IGNORECASE,
)
_SHARE = re.compile(
    rf"\b(?:each|every)\s+(?P<words>[a-z]+(?:\s+[a-z]+)?){_APOSTROPHE}s?\s+"
    r"(?:share|percentage|proportion)\b(?:\s+of\s+(?:the\s+)?(?:total|all)\b)?|"
    r"\b(?:share|percentage|percent|proportion)\s+of\s+(?:the\s+)?(?:grand\s+|overall\s+)?total\b",
    re.IGNORECASE,
)
# Checked in this order: a share or a subtotal also reads "total", and "each store's total"
# would read as a total row otherwise.
_PHRASES = (("share", _SHARE), ("subtotals", _SUBTOTALS), ("beside", _BESIDE), ("rows", _ROWS))
# The words a total's phrase leaves behind: "as a" before a share, "of" before its measure.
_FILLER_END = re.compile(r"(?:\s*,)?\s+(?:as\s+an?|as|in|and|with|plus|including)\s*$", re.I)
_FILLER_START = re.compile(r"^\s*(?:of|in)\s+", re.I)
_PRONOUNS = {"one", "it", "its", "their", "them"}
_TOTAL_WORDS = {"total", "subtotal", "share", "percent", "percentage", "proportion"}
_GRAND = re.compile(r"\b(?:grand|overall)\b", re.IGNORECASE)
_ACROSS_PERIODS = (
    "each total row keeps its period, so none is the grand total across the periods (ask "
    "without the period for it, or for each group's total across the periods)"
)
_BEFORE_THE_CUT = (
    "Each total row adds up every row the call keeps before its having and limit, not only the "
    "rows it returns."
)
_WHAT = {
    "rows": "a grand total row",
    "subtotals": "subtotal rows",
    "beside": "each group's total beside its rows",
    "share": "a share of the total",
}


@dataclass(frozen=True)
class TotalsAsk:
    """The total a question asks for, the words that ask for it, and the question without
    them."""

    kind: str  # "rows", "subtotals", "beside" or "share"
    words: str  # the grouping the phrase names ("store"), or ""
    phrase: str
    rest: str
    across: str = ""  # what a group's total adds up across ("months"), or ""


def asked_totals(question: str, config: Any) -> TotalsAsk | None:
    """The total the question asks for, or None: the first phrase found, in ``_PHRASES``
    order. A question that names a catalog object with a total's word ("Order total",
    "Food revenue share") asks for none, nor one with nothing left beside its total words."""
    said = f" {' '.join(_singular(token) for token in _tokens(question))} "
    if any(f" {phrase} " in said for phrase in _catalog_phrases(config)):
        return None
    for kind, pattern in _PHRASES:
        match = pattern.search(question)
        if match is None:
            continue
        groups = match.groupdict()
        words = (groups.get("words") or groups.get("by") or groups.get("per") or "").strip()
        words = "" if words.lower() in _PRONOUNS else words  # "each one's share"
        before = _FILLER_END.sub("", question[: match.start()])
        after = _FILLER_START.sub("", question[match.end() :])
        if kind == "subtotals":
            # Subtotal levels end at the grand total: "and a grand total" asks nothing more.
            before, after = _FILLER_END.sub("", _ROWS.sub("", before)), _ROWS.sub("", after)
        rest = re.sub(r"\s+(?=,)", "", re.sub(r"\s+", " ", f"{before} {after}")).strip(" ,;")
        if kind in {"share", "beside"} and words and words.lower() not in rest.lower():
            # "each store's share of revenue": the question is revenue by store.
            rest = f"{rest} by {words}"
        if not re.search(r"\w", rest):
            return None
        phrase = question[match.start() : match.end()].strip()
        return TotalsAsk(kind, words, phrase, rest, (groups.get("across") or "").strip())
    return None


def _catalog_phrases(config: Any) -> set[str]:
    """The labels, names and aliases of catalog objects that hold a total's word, as tokens."""
    rows = [*config.measures, *config.metric_recipes, *config.dimensions, *config.segments]
    names = (name for row in rows for name in (row.label, row.name, *row.aliases))
    phrases = (" ".join(_singular(token) for token in _tokens(name)) for name in names)
    return {phrase for phrase in phrases if set(phrase.split()) & _TOTAL_WORDS}


def shaped_call(config: Any, call: Call, asked: TotalsAsk) -> tuple[Call | None, str]:
    """``call`` (a ready draft) with the total ``asked`` names, or None and why the draft has
    no place for it."""
    group_by = list(call.group_by)
    grain = call.time.grain if call.time is not None else ""
    # "The grand total" of a series is one number across its periods, which a total row is only
    # when the window holds one period (a balance read on its one day).
    grand = bool(grain) and _GRAND.search(asked.phrase) is not None and not _one_period(call.time)
    if asked.kind in {"rows", "subtotals"} and call.totals is not None:
        return call, ""  # the caller's call already asks for total rows
    named = _named_columns(config, group_by, asked.words, grain)
    if asked.words and not named:
        return None, f"the draft groups by nothing '{asked.words}' names"
    if not group_by:
        if asked.kind == "rows" and not grain:
            return call, ""  # a call grouped by nothing is its own grand total
        if asked.kind == "share" and named == [TIME_AXIS]:
            return _with_items(config, call, "share", named)  # "each month's share"
        if asked.kind == "beside" and named == [TIME_AXIS] and not asked.across:
            return call, ""  # "each month's total": the rows are the totals
        if grain:
            return None, "the draft groups only by period, and a total row adds up groups"
        return None, "the draft groups by nothing, so it has no rows to total"
    builder = QueryBuilder(config, call)
    if asked.kind == "rows":
        if grand:
            return None, _ACROSS_PERIODS
        return builder.totals([]).build(), ""
    if asked.kind == "subtotals":
        if named == [TIME_AXIS]:
            return builder.totals([]).build(), ""  # each total row keeps its period
        kept = named or _with_partners(config, group_by, group_by[:1])
        if not set(kept) >= set(group_by):
            return builder.totals(_totals_levels(config, group_by, kept)).build(), ""
        if not grain:
            return builder.totals([]).build(), ""
        # Total rows keep the period: each group's total across the periods is a total
        # beside its rows.
        return _with_items(config, call, "total", [TIME_AXIS])
    if asked.kind == "share":
        over = named or [*group_by, *([TIME_AXIS] if grand else [])]
        return _with_items(config, call, "share", over)
    if not named:
        return None, "the question names no grouping whose total to show"
    # A group's total beside each of its rows: the call without its other columns, or without
    # the ones the question says it adds up across ("each store's total across types").
    others = [column for column in group_by if column not in named]
    others += [TIME_AXIS] if grain and TIME_AXIS not in named else []
    over = others
    if asked.across:
        over = _named_columns(config, group_by, asked.across, grain)
        if not over or not set(over) <= set(others):
            return None, f"'{asked.across}' names none of the draft's other groupings"
    return _with_items(config, call, "total", over) if over else (call, "")  # rows are totals


def _one_period(time: Time | None) -> bool:
    """Whether the call's window holds exactly one period of its grain."""
    if time is None:
        return False
    if time.last is not None:
        return time.last == (time.grain, 1)
    if time.this:
        return time.this == time.grain
    if not (len(time.start) == 10 and len(time.end) == 10):
        return False  # an open window, or a bound inside a day
    try:
        start, end = date.fromisoformat(time.start), date.fromisoformat(time.end)
        return floor_period(start, time.grain) == start and period_end(start, time.grain) == end
    except (ValueError, SemanticLayerError):
        return False  # not a date, or a grain finer than a day


def _named_columns(config: Any, group_by: list[str], words: str, grain: str) -> list[str]:
    """The call's columns ``words`` name: a group_by dimension by its own words, or an
    entity's key and name by the entity's; ``period`` by the grain's word."""
    wanted = _singulars(words)
    if not wanted:
        return []
    if grain and wanted <= {grain, "period"}:
        return [TIME_AXIS]
    dimensions = {row.id: row for row in config.dimensions}
    named = [
        column
        for column in group_by
        if column in dimensions
        and wanted <= _singulars(" ".join([dimensions[column].label, dimensions[column].name]))
    ]
    for entity, keys, shown in _stand_ins(config, group_by):
        if wanted <= _singulars(" ".join([entity.label, entity.name, *entity.aliases])):
            named.extend([*keys, *shown])
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
    """``columns`` with the key or name grouped beside each (one grouping, two columns), in
    group_by order."""
    chosen = set(columns)
    for _entity, keys, shown in _stand_ins(config, group_by):
        if chosen & {*keys, *shown}:
            chosen |= {*keys, *shown}
    return [column for column in group_by if column in chosen]


def _totals_levels(config: Any, group_by: list[str], kept: list[str]) -> list[str]:
    """The ``totals`` that keep ``kept``: an entity's name without its key, since a level
    keeps the key of each name it keeps (naming the key too would add a level of its own)."""
    keys = {
        key
        for _entity, entity_keys, shown in _stand_ins(config, group_by)
        if shown and set(shown) <= set(kept)
        for key in entity_keys
    }
    return [column for column in kept if column not in keys]


def _with_items(config: Any, call: Call, kind: str, over: list[str]) -> tuple[Call | None, str]:
    """``call`` with a ``kind`` item over ``over`` beside each measure or metric it selects."""
    builder = QueryBuilder(config, call)
    for item in call.select:
        source = item.expression
        if not isinstance(source, str) or not source.startswith(("measure.", "metric.")):
            return None, f"a {kind} of a computed column isn't drafted"
        builder.select({"kind": kind, "input": source, "over": list(over)}, f"{item.alias}_{kind}")
    return builder.build(), ""


def with_totals(
    runtime: Any, payload: dict[str, Any], asked: TotalsAsk, question: str
) -> dict[str, Any]:
    """``payload``, the plan of ``asked.rest``, as the plan of ``question``: a ready draft
    carries the total, validated as execute reads it, or the plan is held with why not."""
    payload = {**payload, "intent": question}
    if isinstance(payload.get("intent_ir"), dict):
        totals = {"kind": asked.kind, "phrase": asked.phrase}
        payload["intent_ir"] = {**payload["intent_ir"], "totals": totals}
    best = payload.get("best") or {}
    query = best.get("query")
    if payload["status"] != "ok" or not isinstance(query, dict):
        # The draft is held already; it says so beside the plan's own why.
        return _not_drafted(payload, asked, "plan drafts it once the draft is ready", held=False)
    draft = Call.from_call(query, runtime._config.measures)
    try:
        shaped, reason = shaped_call(runtime._config, draft, asked)
    except SemanticLayerError as exc:  # the builder's own check: never a call to hand back
        shaped, reason = None, f"the draft can't carry it ({exc.code})"
    if shaped is None:
        return _not_drafted(payload, asked, reason)
    if shaped is draft:
        return payload
    validation = _validate_query(runtime, shaped)
    if not validation["ok"]:
        error = (validation.get("errors") or [{}])[0]
        refusal = {key: error[key] for key in ("code", "message") if error.get(key)}
        path = error.get("path") or (error.get("details") or {}).get("path")
        if path:
            refusal["path"] = path
        reason = f"the engine refuses it ({error.get('code')})"
        return _not_drafted(payload, asked, reason, refusal=refusal)
    payload["best"] = {**best, "query": shaped.to_call()}
    if asked.kind in {"rows", "subtotals"} and (shaped.limit is not None or shaped.having):
        # "Top 3 stores with the grand total": the total of every store, not of the 3.
        payload["assumptions"] = [*payload.get("assumptions", []), _BEFORE_THE_CUT]
    return payload


def _not_drafted(
    payload: dict[str, Any],
    asked: TotalsAsk,
    reason: str,
    *,
    held: bool = True,
    refusal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``payload`` with ``PLAN_TOTALS_NOT_DRAFTED``: its draft answers the question without
    the total; ``held`` makes that the plan's why, so it isn't ready."""
    message = f"The question asks for {_WHAT[asked.kind]} ('{asked.phrase}'), but {reason}"
    if held:
        message += ", so best.query answers the question without it and plan doesn't call it ready"
    details: dict[str, Any] = {"kind": asked.kind, "phrase": asked.phrase}
    if refusal:
        details["refusal"] = refusal
    why = {"code": "PLAN_TOTALS_NOT_DRAFTED", "message": message + ".", "details": details}
    warnings = [*payload.get("warnings", []), {**why, "severity": "warning"}]
    payload = {**payload, "warnings": warnings}
    if held:
        next_steps = {k: v for k, v in (payload.get("next") or {}).items() if k != "ready_for"}
        payload.update(status="low_confidence", why=why, next=next_steps)
    return payload
