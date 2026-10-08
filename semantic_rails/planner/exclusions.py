"""Exclusion clauses: the one reader of "excluding X" text, and the check that a draft drops
exactly what each clause names.

A marker from a closed list ("excluding", "except", "without", "not", "but not", "other than",
"apart from", "aside from", "minus", "outside of", "all … but") opens a list,
``lead? item (separator lead? item)*``. An item is one time phrase the time reader found, one
quoted string, or one declared value name (its value, label or alias, longest first); any other
word in an item's place is an ``unknown`` item. The list ends at the first token that is
neither an item nor a separator. The first time phrase after it with only words between
("excluding web in June 2024") stays a positive window; every other value, quoted or time
mention up to the clause's end (the next marker, an "including", or the end of the question)
is an ``unknown`` item, and a clause with no item gets one.

``unrealized`` holds every item to its own predicate: a value item is realized only by an outer
``IS DISTINCT FROM`` filter on its one bound dimension, since an exclusion keeps rows with no
recorded value and ``!=`` or ``NOT IN`` drop them, with no value the question doesn't name
excluded beside it. Query IR has no window complement, so a time item is never realized.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..ast import is_child_group
from .coverage import CoverageGap, _dict_nodes, _is_number
from .visibility import visible_value_domains

_MARKER_RE = re.compile(
    r"\b(?:excluding|except|without|but\s+not|not\b(?!\s+only\b)(?:\s+includ(?:ing|es?))?|"
    r"other\s+than|apart\s+from|aside\s+from|minus|outside\s+of)\b"
)
# "for all stores but Brooklyn": "but" excludes after all/every/each/any.
_ALL_BUT_RE = re.compile(
    r"\b(?:all|every|each|any)\s+(?:[^\W\d_][\w-]*\s+){0,3}?(but)\b(?!\s+not\b)"
)
# An explicit inclusion ends the clause; "not including" is itself a marker.
_INCLUSION_RE = re.compile(r"\b(?:including|include|includes)\b")
# Words, newlines and single marks; apostrophes, hyphens and underscores join words.
_TOKEN_RE = re.compile(r"[^\W_]+|\n|[^\w\s'’-]")
_SEPARATOR_WORDS = frozenset({"and", "or", "nor", "plus", "alongside"})
_SEPARATOR_MARKS = frozenset({",", ";", "/", "&", "(", "—", "–", "\n"})
_SEPARATOR_PHRASES = (("as", "well", "as"), ("along", "with"), ("together", "with"))
_QUOTES = frozenset({'"', "“", "”"})
_WEEKDAYS = frozenset(
    {
        *("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"),
        *("mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun"),
    }
)
_LEADS = frozenset({"in", "on", "for", "during", "from", "the", *_WEEKDAYS})

Span = tuple[int, int]
Names = dict[str, list[tuple[tuple[str, ...], dict[str, tuple[Any, ...]]]]]


@dataclass(frozen=True)
class _Token:
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class ExcludedItem:
    """One thing a clause excludes: a declared value, a time phrase, or something unread."""

    kind: str  # "value" | "time" | "unknown"
    span: Span
    text: str = ""
    # Each dimension the item's name binds, with the values it names there.
    dimensions: dict[str, tuple[Any, ...]] = field(default_factory=dict)

    @property
    def binding(self) -> tuple[str, Any] | None:
        """The one (dimension, value) a value item names, else ``None``."""

        if self.kind != "value" or len(self.dimensions) != 1:
            return None
        ((dimension, values),) = self.dimensions.items()
        return (dimension, values[0]) if len(values) == 1 else None


@dataclass(frozen=True)
class ExclusionClause:
    marker: Span
    text: str
    items: tuple[ExcludedItem, ...]
    end: int = 0  # where the clause ends: the next marker, an "including", or the question's end


@dataclass(frozen=True)
class _Region:
    marker: Span
    end: int
    first: int  # the first token after the marker
    window: Span | None  # the positive window after the list


def _tokenize(lowered: str) -> list[_Token]:
    return [_Token(match.group(0), *match.span()) for match in _TOKEN_RE.finditer(lowered)]


def _markers(lowered: str) -> list[Span]:
    spans = sorted(
        [match.span() for match in _MARKER_RE.finditer(lowered)]
        + [match.span(1) for match in _ALL_BUT_RE.finditer(lowered)]
    )
    out: list[Span] = []
    for span in spans:
        if not out or span[0] >= out[-1][1]:
            out.append(span)
    return out


def _separator_at(tokens: list[_Token], index: int) -> int:
    """How many tokens the separator at ``index`` takes, 0 when there is none."""

    text = tokens[index].text
    if text in _SEPARATOR_WORDS or text in _SEPARATOR_MARKS:
        return 1
    for phrase in _SEPARATOR_PHRASES:
        if tuple(token.text for token in tokens[index : index + len(phrase)]) == phrase:
            return len(phrase)
    return 0


def _index_at(tokens: list[_Token], offset: int, start: int = 0) -> int:
    """The first token at or after character ``offset``."""

    index = start
    while index < len(tokens) and tokens[index].start < offset:
        index += 1
    return index


def _word(token: _Token) -> bool:
    return token.text[0].isalnum()


class _Lists:
    """Reads lists of items. Without the catalog's ``names`` every word is an item: the reading
    ``time_windows`` takes, which decides where a list ends without knowing values."""

    def __init__(self, tokens: list[_Token], time_spans: list[Span], names: Names | None) -> None:
        self.tokens = tokens
        self.time_spans = list(time_spans)
        self.time_ends = {start: end for start, end in sorted(time_spans, reverse=True)}
        self.names = names

    def value_at(
        self, index: int, stop: int, *, exact: bool = False
    ) -> tuple[int, dict[str, tuple[Any, ...]]] | None:
        """The longest value name starting at ``index`` and ending by ``stop`` (exactly at
        ``stop`` when ``exact``); a plural of its last word counts unless ``exact``."""

        tokens = self.tokens
        if index >= stop:
            return None
        if self.names is None:
            plain = _word(tokens[index]) and tokens[index].text not in _LEADS
            return (index + 1, {}) if plain and (not exact or index + 1 == stop) else None
        for words, dimensions in self.names.get(tokens[index].text, []):
            end = index + len(words)
            if end > stop or (exact and end != stop):
                continue
            said = [token.text for token in tokens[index:end]]
            last = words[-1]
            plural = not exact and last.isalpha() and said[-1] in {f"{last}s", f"{last}es"}
            if said[:-1] == list(words[:-1]) and (said[-1] == last or plural):
                return end, dimensions
        return None

    def item_at(self, index: int, limit: int) -> tuple[int, ExcludedItem] | None:
        """A quoted string, a time phrase or a value name starting at token ``index``."""

        tokens = self.tokens
        start = tokens[index].start
        if tokens[index].text in _QUOTES:
            close = next(
                (
                    number
                    for number in range(index + 1, len(tokens))
                    if tokens[number].start < limit and tokens[number].text in _QUOTES
                ),
                None,
            )
            if close is not None:
                # A quoted string is always an item, matched whole on its own words.
                value = self.value_at(index + 1, close, exact=True)
                kind = "value" if value is not None and self.names is not None else "unknown"
                dimensions = value[1] if value is not None else {}
                return close + 1, ExcludedItem(kind, (start, tokens[close].end), "", dimensions)
        stop = _index_at(tokens, limit, index)
        time_end = self.time_ends.get(start)
        value = self.value_at(index, stop)
        if value is not None and (time_end is None or tokens[value[0] - 1].end > time_end):
            span = (start, tokens[value[0] - 1].end)
            if any(span[0] < end and begin < span[1] for begin, end in self.time_spans):
                # A value name holding a time phrase is still a time phrase.
                return value[0], ExcludedItem("time", span)
            kind = "value" if self.names is not None else "unknown"
            return value[0], ExcludedItem(kind, span, "", value[1])
        if time_end is not None:
            return _index_at(tokens, time_end, index), ExcludedItem("time", (start, time_end))
        return None

    def slot(self, index: int, limit: int) -> tuple[int, ExcludedItem] | None:
        """The item in a list slot. Separators and leads before it are skipped, and a word that
        is no item is an unknown one."""

        tokens = self.tokens
        while index < len(tokens) and tokens[index].start < limit:
            found = self.item_at(index, limit)
            if found is not None:
                return found
            taken = _separator_at(tokens, index)
            if taken:
                index += taken
            elif tokens[index].text in _LEADS:
                index += 1
                # "on Tue. June 25": a weekday's own mark leads in too.
                weekday = tokens[index - 1].text in _WEEKDAYS
                if weekday and index < len(tokens) and tokens[index].text in {".", ","}:
                    index += 1
            else:
                break
        if index < len(tokens) and tokens[index].start < limit and _word(tokens[index]):
            token = tokens[index]
            return index + 1, ExcludedItem("unknown", (token.start, token.end))
        return None

    def read(self, first: int, limit: int) -> tuple[list[ExcludedItem], int]:
        """The list's items, and the index of the token after it."""

        tokens = self.tokens
        items: list[ExcludedItem] = []
        index = first
        while True:
            found = self.slot(index, limit)
            if found is None:
                return items, index
            index, item = found
            items.append(item)
            separated = False
            while index < len(tokens) and tokens[index].start < limit:
                taken = _separator_at(tokens, index)
                if not taken:
                    break
                index += taken
                separated = True
            if not separated:
                # A closing bracket ends the list it closes.
                if index < len(tokens) and tokens[index].text == ")":
                    index += 1
                return items, index


def _trailing_window(
    tokens: list[_Token], index: int, limit: int, time_spans: list[Span]
) -> Span | None:
    """The first time phrase after the list, with only words between ("excluding web in June
    2024", "not from Brooklyn last month"); a separator or a mark first means there is none."""

    starts = {start: (start, end) for start, end in time_spans}
    while index < len(tokens) and tokens[index].start < limit:
        token = tokens[index]
        if token.start in starts:
            return starts[token.start]
        if _separator_at(tokens, index) or not _word(token):
            return None
        index += 1
    return None


def _regions(lowered: str, tokens: list[_Token], time_spans: list[Span]) -> list[_Region]:
    """Each marker's clause and the positive window after its list, read without the catalog
    so that the time reader and the matcher agree on the window."""

    markers = _markers(lowered)
    out: list[_Region] = []
    for number, marker in enumerate(markers):
        end = markers[number + 1][0] if number + 1 < len(markers) else len(lowered)
        inclusion = _INCLUSION_RE.search(lowered, marker[1], end)
        if inclusion is not None:
            end = inclusion.start()
        first = _index_at(tokens, marker[1])
        _items, stop = _Lists(tokens, time_spans, None).read(first, end)
        out.append(_Region(marker, end, first, _trailing_window(tokens, stop, end, time_spans)))
    return out


def excluded_time_spans(lowered: str, time_spans: list[Span]) -> list[Span]:
    """Time phrases inside an exclusion clause other than its positive window: never a window
    the question asks for."""

    if not _markers(lowered):
        return []
    return sorted(
        span
        for region in _regions(lowered, _tokenize(lowered), time_spans)
        for span in time_spans
        if region.marker[1] <= span[0] < region.end and span != region.window
    )


def exclusion_markers(text: str) -> list[Span]:
    """Where each exclusion marker sits ("excluding", "other than")."""

    return _markers(str(text or "").lower())


def exclusion_regions(text: str) -> list[Span]:
    """Where each exclusion clause sits, from its marker to its end."""

    from .time_windows import _time_window  # noqa: WPS433 - time_windows reads this module

    lowered = str(text or "").lower()
    if not _markers(lowered):
        return []
    spans = list(_time_window(str(text or "")).spans)
    return [
        (region.marker[0], region.end) for region in _regions(lowered, _tokenize(lowered), spans)
    ]


def _value_names(config: Any) -> Names:
    """Each declared value name as tokens, longest first, with the values it names."""

    named: dict[tuple[str, ...], dict[str, list[Any]]] = {}
    for domain in visible_value_domains(config):
        for value in list(domain.values or []):
            if _is_number(value.value):
                continue
            for name in (value.value, value.label, *(value.aliases or [])):
                words = tuple(token.text for token in _tokenize(str(name or "").lower()))
                if not words or _is_number(name):
                    continue
                for dimension in (str(item) for item in domain.dimensions):
                    values = named.setdefault(words, {}).setdefault(dimension, [])
                    if not _contains_literal(values, value.value):
                        values.append(value.value)
    out: Names = {}
    for words, dimensions in sorted(named.items(), key=lambda row: -len(row[0])):
        out.setdefault(words[0], []).append(
            (words, {dimension: tuple(values) for dimension, values in dimensions.items()})
        )
    return out


def exclusion_clauses(
    config: Any, text: str, policy_context: dict[str, Any] | None = None
) -> list[ExclusionClause]:
    """Every exclusion clause of the question, with its typed items."""

    from .time_windows import _time_window  # noqa: WPS433 - time_windows reads this module

    text = str(text or "")
    lowered = text.lower()
    if not _markers(lowered):
        return []
    if len(lowered) != len(text):
        # Lowercasing moved the offsets: nothing in the question can be read safely.
        unread = ExcludedItem("unknown", (0, len(text)), text)
        return [ExclusionClause((0, 0), text, (unread,), len(text))]
    window = _time_window(text, policy_context=policy_context)
    time_spans = list(window.spans)
    excluded = {*window.excluded, *excluded_time_spans(lowered, time_spans)}
    tokens = _tokenize(lowered)
    lists = _Lists(tokens, time_spans, _value_names(config))
    clauses: list[ExclusionClause] = []
    for region in _regions(lowered, tokens, time_spans):
        items, index = lists.read(region.first, region.end)
        # Every other value, quoted or time mention up to the clause's end is unread.
        while index < len(tokens) and tokens[index].start < region.end:
            if (
                region.window is not None
                and region.window[0] <= tokens[index].start < region.window[1]
            ):
                index += 1
                continue
            found = lists.item_at(index, region.end)
            if found is None:
                index += 1
                continue
            index, item = found
            items.append(ExcludedItem("unknown", item.span, "", item.dimensions))
        items += [
            ExcludedItem("unknown", span)
            for span in sorted(excluded)
            if region.marker[1] <= span[0] < region.end
            and not any(item.span[0] <= span[0] < item.span[1] for item in items)
        ]
        if not items:
            items = [ExcludedItem("unknown", region.marker)]
        end = max(item.span[1] for item in items)
        clauses.append(
            ExclusionClause(
                region.marker,
                text[region.marker[0] : max(end, region.marker[1])].strip(),
                tuple(
                    ExcludedItem(
                        item.kind,
                        item.span,
                        text[item.span[0] : item.span[1]],
                        item.dimensions,
                    )
                    for item in sorted(items, key=lambda item: item.span)
                ),
                region.end,
            )
        )
    return clauses


def declared_values(config: Any, *, limit: int = 25) -> dict[str, list[Any]]:
    """Each dimension's declared values (at most ``limit`` each), for a gap to list."""

    out: dict[str, list[Any]] = {}
    for domain in visible_value_domains(config):
        for dimension in (str(item) for item in domain.dimensions):
            values = out.setdefault(dimension, [])
            for value in list(domain.values or []):
                if len(values) < limit and not _contains_literal(values, value.value):
                    values.append(value.value)
    return out


def _contains_literal(literals: list[Any] | tuple[Any, ...], canonical: Any) -> bool:
    """SQL string equality has no catalog-label, case or punctuation rewrite."""

    return any(type(item) is type(canonical) and item == canonical for item in literals)


_KEEPING = frozenset({"=", "==", "IN"})
_DROPPING = frozenset({"!=", "<>", "NOT IN"})
_KEEPS_UNRECORDED = "IS DISTINCT FROM"


def _read_filter(row: dict[str, Any]) -> tuple[str, list[Any]] | None:
    """An exact scalar comparison or membership as (op, literals); anything else is None."""

    op = " ".join(str(row.get("op") or "=").upper().split())
    raw = row.get("value")
    if op in {"IN", "NOT IN"}:
        literals = raw if isinstance(raw, list) else [raw]
    elif op in _KEEPING | _DROPPING | {_KEEPS_UNRECORDED}:
        literals = [raw]
    else:
        return None
    if not literals or any(
        item is None or isinstance(item, (list, tuple, dict)) for item in literals
    ):
        return None
    return op, literals


def _scoped_fields(query: dict[str, Any]) -> set[str]:
    """Fields a child group, a selected expression or any other compound condition reads in a
    scope of its own."""

    out: set[str] = set()
    for node in list(query.get("where") or []):
        plain = isinstance(node, dict) and isinstance(node.get("field"), str)
        if is_child_group(node) or not plain:
            out.update(
                row["field"] for row in _dict_nodes(node) if isinstance(row.get("field"), str)
            )
    nested = {key: value for key, value in query.items() if key != "where"}
    for node in _dict_nodes(nested):
        if isinstance(node.get("field"), str) and ("op" in node or "value" in node):
            out.add(node["field"])
    return out


def _positive_values(
    config: Any, text: str, clauses: list[ExclusionClause]
) -> dict[str, list[Any]]:
    """The values the question names outside every exclusion clause, by dimension."""

    tokens = _tokenize(str(text or "").lower())
    lists = _Lists(tokens, [], _value_names(config))
    out: dict[str, list[Any]] = {}
    index = 0
    while index < len(tokens):
        start = tokens[index].start
        found = None
        if not any(clause.marker[0] <= start < clause.end for clause in clauses):
            found = lists.value_at(index, len(tokens))
        if found is None:
            index += 1
            continue
        index, dimensions = found
        for dimension, values in dimensions.items():
            out.setdefault(dimension, []).extend(values)
    return out


def exclusion_gaps(
    config: Any, text: str, query: dict[str, Any], *, caller: dict[str, Any] | None = None
) -> list[CoverageGap]:
    """The question's exclusion clauses that the draft doesn't realize item by item."""

    clauses = exclusion_clauses(config, text, query.get("policy_context"))
    if not clauses:
        return []
    return unrealized(
        clauses,
        query,
        caller=caller,
        positive=_positive_values(config, text, clauses),
        valid_values=declared_values(config),
    )


def unrealized(
    clauses: list[ExclusionClause],
    query: dict[str, Any],
    *,
    caller: dict[str, Any] | None = None,
    positive: dict[str, list[Any]] | None = None,
    valid_values: dict[str, list[Any]] | None = None,
) -> list[CoverageGap]:
    """One gap for each clause the draft doesn't realize item by item.

    A value item needs an outer ``IS DISTINCT FROM`` filter on its value, on its one bound
    dimension, which no other scope conditions. No outer filter may exclude a value the question
    doesn't name, and on an item's dimension a filter that keeps values keeps only values the
    question names (``positive``, outside its exclusions). The caller's own ``partial_query``
    filters may exclude or keep other values, but realize an item only by dropping that value.
    """

    if not clauses:
        return []
    where = list(query.get("where") or [])
    caller_rows = [row for row in list((caller or {}).get("where") or []) if isinstance(row, dict)]
    named: dict[str, list[Any]] = {}
    for clause in clauses:
        for item in clause.items:
            if item.binding is not None:
                named.setdefault(item.binding[0], []).append(item.binding[1])
    positive = positive or {}
    scoped = _scoped_fields(query)
    filters = [
        (f"where[{index}]", row, _read_filter(row))
        for index, row in enumerate(where)
        if isinstance(row, dict) and not is_child_group(row) and isinstance(row.get("field"), str)
    ]
    excess = [
        {"path": path, "field": row["field"], "value": literal}
        for path, row, read in filters
        if read is not None
        and row not in caller_rows
        and (read[0] not in _KEEPING or row["field"] in named)
        for literal in read[1]
        if not _contains_literal(named.get(row["field"], []), literal)
        and not (read[0] in _KEEPING and _contains_literal(positive.get(row["field"], []), literal))
    ]
    gaps: list[CoverageGap] = []
    for number, clause in enumerate(clauses):
        report: dict[str, list[Any]] = {
            "matched": [],
            "missing": [],
            "unresolved": [],
            "drops_rows_without_a_value": [],
            "positive_matches": [],
        }
        fields: set[str] = set()
        for item in clause.items:
            if item.binding is None:
                reason = "ambiguous" if item.kind == "value" else item.kind
                report["unresolved"].append({"text": item.text, "reason": reason})
                continue
            field_id, canonical = item.binding
            fields.add(field_id)
            rows = [(path, row, read) for path, row, read in filters if row["field"] == field_id]
            if field_id in scoped or any(read is None for _path, _row, read in rows):
                report["missing"].append(item.text)
                continue
            readable = [(path, row, *read) for path, row, read in rows if read is not None]
            keeping = [(row, literals) for _p, row, op, literals in readable if op in _KEEPING]
            on_value = [
                (path, op)
                for path, _row, op, literals in readable
                if _contains_literal(literals, canonical)
            ]
            dropped = [path for path, op in on_value if op in _DROPPING]
            realized = [path for path, op in on_value if op == _KEEPS_UNRECORDED]
            kept = bool(keeping) and all(_contains_literal(lits, canonical) for _r, lits in keeping)
            if kept and not (dropped or realized):
                # Every filter that keeps values keeps this one: the request reversed.
                report["positive_matches"] += [row for row, _literals in keeping]
            elif dropped:
                report["drops_rows_without_a_value"] += dropped
            elif realized:
                report["matched"] += realized
            else:
                report["missing"].append(item.text)
        extra = [
            row
            for row in excess
            if row["field"] in fields or (number == 0 and row["field"] not in named)
        ]
        if not (extra or any(report[key] for key in report if key != "matched")):
            continue
        gaps.append(_clause_gap(clause, where, report, extra, valid_values))
    return gaps


def _clause_gap(
    clause: ExclusionClause,
    where: list[Any],
    report: dict[str, list[Any]],
    excess: list[dict[str, Any]],
    valid_values: dict[str, list[str]] | None,
) -> CoverageGap:
    reasons = {row["reason"] for row in report["unresolved"]}
    expected: dict[str, Any] = {
        "filter_polarity": "negative",
        "excluded_text": clause.text,
        "items": [
            {"text": item.text, "kind": item.kind}
            | ({"field": item.binding[0], "value": item.binding[1]} if item.binding else {})
            for item in clause.items
        ],
        "rows_without_a_value": "kept",
    }
    hints = [
        "Exclude each named value with its own where filter {field, op: 'IS DISTINCT FROM', "
        "value}; it keeps rows with no recorded value, which '!=' and 'NOT IN' drop. Exclude "
        "nothing the question doesn't name."
    ]
    if "time" in reasons:
        hints.append(
            "Query IR has no window complement: ask for the windows before and after the "
            "excluded period instead."
        )
    if reasons & {"unknown", "ambiguous"}:
        hints.append("Name each excluded value as valid_values spells it.")
        if valid_values:
            expected["valid_values"] = valid_values
    positive = bool(report["positive_matches"])
    return CoverageGap(
        kind="negation_reversed" if positive else "negation_unrealized",
        clause=clause.text,
        message=(
            "The excluded value is encoded by a positive filter, reversing the request."
            if positive
            else "The draft doesn't drop exactly what the exclusion names, keeping rows with no "
            "recorded value."
        ),
        expected=expected,
        actual={"where": where, **report, "excess": excess},
        recovery_hint={"kind": "provide_negative_filter", "message": " ".join(hints)},
    )


__all__ = [
    "ExcludedItem",
    "ExclusionClause",
    "declared_values",
    "excluded_time_spans",
    "exclusion_clauses",
    "exclusion_gaps",
    "exclusion_markers",
    "exclusion_regions",
    "unrealized",
]
