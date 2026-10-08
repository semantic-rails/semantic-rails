"""Exclusion clauses: the one reader of "excluding X" text, and the check that a draft drops
exactly what each clause names.

A marker from a closed list ("excluding", "except", "without", "not", "but not", "other than",
"apart from", "aside from", "minus", "outside of", "all … but") opens a list,
``lead? item (separator lead? item)*``. An item is one time phrase the time reader found, one
double-quoted string, or one declared value name (its value, label or alias, longest first);
any other word in an item's place is an ``unknown`` item. The list ends at the first token
that is neither an item nor a separator. Every other character up to the next word must
belong to an item, a separator or a lead; each run that doesn't is an ``unknown`` item, so a
single-quoted name ('store') or one with no word ("-") holds. The question's final ".", "?"
or "!" is the only exception. The first time phrase after the list with only words between
("excluding web in June 2024") stays a positive window; every other value, quoted or time
mention up to the clause's end (the next marker, an "including", or the end of the question)
is an ``unknown`` item, and a clause with no item gets one. A marker or "including" inside a
quoted string or a declared name ("Including Top") leaves the whole question unread.

``unrealized`` holds every item to its own predicate: a value item is realized only by an outer
``IS DISTINCT FROM`` filter on its one bound dimension, since an exclusion keeps rows with no
recorded value and ``!=`` or ``NOT IN`` drop them. Beside an exclusion the draft, whoever
supplied it, carries only a closed list of parts (``_excess``); anything else holds. Query IR
has no window complement, so a time item is never realized.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..ast import is_child_group
from .coverage import CoverageGap, _is_number
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
# A quoted span: double quotes, or single ones whose marks touch no word on their outer side.
_QUOTED_RE = re.compile(r"[\"“”][^\"“”]*[\"“”]|(?<!\w)['‘].*?['’](?!\w)", re.DOTALL)
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
    separators: tuple[Span, ...] = ()


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
        # The separators the lists read, which the exclusion check accounts for.
        self.separators: list[Span] = []
        # The leads ("on Tue.") and closing brackets the lists read.
        self.leads: list[Span] = []

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
        first = tokens[index].text
        # A one-word name may be said in the plural ("stores").
        candidates = [
            *self.names.get(first, []),
            *(row for row in self.names.get(first[:-1], []) if len(row[0]) == 1),
            *(row for row in self.names.get(first[:-2], []) if len(row[0]) == 1),
        ]
        for words, dimensions in sorted(candidates, key=lambda row: -len(row[0])):
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
                self.separators.append((tokens[index].start, tokens[index + taken - 1].end))
                index += taken
            elif tokens[index].text in _LEADS:
                self.leads.append((tokens[index].start, tokens[index].end))
                index += 1
                # "on Tue. June 25": a weekday's own mark leads in too.
                weekday = tokens[index - 1].text in _WEEKDAYS
                if weekday and index < len(tokens) and tokens[index].text in {".", ","}:
                    self.leads.append((tokens[index].start, tokens[index].end))
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
                self.separators.append((tokens[index].start, tokens[index + taken - 1].end))
                index += taken
                separated = True
            if not separated:
                # A closing bracket ends the list it closes.
                if index < len(tokens) and tokens[index].text == ")":
                    self.leads.append((tokens[index].start, tokens[index].end))
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
        lists = _Lists(tokens, time_spans, None)
        _items, stop = lists.read(first, end)
        window = _trailing_window(tokens, stop, end, time_spans)
        out.append(_Region(marker, end, first, window, tuple(lists.separators)))
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


def _question_regions(text: str, time_spans: Any) -> list[_Region]:
    lowered = str(text or "").lower()
    if not _markers(lowered):
        return []
    return _regions(lowered, _tokenize(lowered), list(time_spans))


def exclusion_words(text: str, time_spans: Any) -> list[Span]:
    """Where each exclusion marker ("excluding", "other than") and each separator of its list
    ("as well as") sits: words the exclusion check accounts for. ``time_spans`` are the time
    reader's spans of the question (``_time_window(text).spans``)."""

    regions = _question_regions(text, time_spans)
    return sorted(span for region in regions for span in (region.marker, *region.separators))


def exclusion_regions(text: str, time_spans: Any) -> list[Span]:
    """Where each exclusion clause sits, from its marker to its end."""

    return [(region.marker[0], region.end) for region in _question_regions(text, time_spans)]


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


def _unread(text: str) -> list[ExclusionClause]:
    """One clause whose one item is the whole question, unread."""

    unread = ExcludedItem("unknown", (0, len(text)), text)
    return [ExclusionClause((0, 0), text, (unread,), len(text))]


def _marker_inside_a_name(lowered: str, tokens: list[_Token], names: Names) -> bool:
    """Whether a marker or an "including" sits inside a quoted string or a declared value
    name ("Including Top", "All but Web"), where reading it would cut the name apart."""

    spans = [match.span() for match in _QUOTED_RE.finditer(lowered)]
    lists = _Lists(tokens, [], names)
    for index, token in enumerate(tokens):
        found = lists.value_at(index, len(tokens))
        if found is not None:
            spans.append((token.start, tokens[found[0] - 1].end))
    words = [
        match.span()
        for pattern in (_MARKER_RE, _ALL_BUT_RE, _INCLUSION_RE)
        for match in pattern.finditer(lowered)
    ]
    return any(start < end and begin < stop for start, stop in words for begin, end in spans)


_RUN_RE = re.compile(r"[^\0\s](?:[^\0]*[^\0\s])?")


def _unread_runs(lowered: str, start: int, stop: int, read: list[Span]) -> list[Span]:
    """Each run of characters from ``start`` to ``stop`` that no ``read`` span covers, without
    its outer whitespace. The question's final ".", "?" or "!" is read."""

    last = len(lowered.rstrip()) - 1
    if last >= 0 and lowered[last] in ".?!":
        read = [*read, (last, last + 1)]
    masked = list(lowered)
    for begin, end in read:
        masked[begin:end] = "\0" * (end - begin)
    return [match.span() for match in _RUN_RE.finditer("".join(masked), start, stop)]


def exclusion_clauses(config: Any, text: str, window: Any) -> list[ExclusionClause]:
    """Every exclusion clause of the question, with its typed items. ``window`` is the time
    reader's reading of the question (``_time_window``), which reads this module."""

    text = str(text or "")
    lowered = text.lower()
    if not _markers(lowered):
        return []
    if len(lowered) != len(text):
        # Lowercasing moved the offsets: nothing in the question can be read safely.
        return _unread(text)
    tokens = _tokenize(lowered)
    names = _value_names(config)
    if _marker_inside_a_name(lowered, tokens, names):
        return _unread(text)
    time_spans = list(window.spans)
    excluded = {*window.excluded, *excluded_time_spans(lowered, time_spans)}
    lists = _Lists(tokens, time_spans, names)
    clauses: list[ExclusionClause] = []
    for region in _regions(lowered, tokens, time_spans):
        items, index = lists.read(region.first, region.end)
        # The list runs to the next word it didn't read.
        stop = next((token.start for token in tokens[index:] if _word(token)), region.end)
        stop = min(stop, region.end)
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
        # Every other character of the list is unread: nothing in it may vanish.
        read = [item.span for item in items] + lists.separators + lists.leads
        if region.window is not None:
            read.append(region.window)
        items += [
            ExcludedItem("unknown", span)
            for span in _unread_runs(lowered, region.marker[1], stop, read)
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


def _declared_values(config: Any, *, limit: int = 25) -> dict[str, list[Any]]:
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


def _plain_filter(node: Any) -> bool:
    """A top-level ``where`` filter on a field, not a child group or a compound condition."""

    return (
        isinstance(node, dict) and not is_child_group(node) and isinstance(node.get("field"), str)
    )


# Draft keys that change no number: syntax, context and ordering, with any value.
_INERT_KEYS = frozenset(
    {
        *("version", "policy_context", "request_context", "request_id", "verbosity"),
        *("sql_profile", "debug", "explain", "order_by"),
    }
)
_BOUNDS = frozenset({"start", "end", "range"})


def _by_id(rows: Any, object_id: Any) -> Any:
    return next((row for row in list(rows or []) if getattr(row, "id", None) == object_id), None)


def _plain_ref(config: Any, item: Any) -> bool:
    """A select item that is one plain reference: ``{metric}``, or ``{measure}`` at the
    measure's own default aggregation, with ``kind`` absent or naming the same."""

    if not isinstance(item, dict) or not set(item) <= {"expression", "as"}:
        return False
    expression = item.get("expression")
    if not isinstance(expression, dict):
        return False
    parts = set(expression) - {"kind"}
    if parts == {"metric"}:
        return expression.get("kind", "metric") == "metric"
    if parts not in ({"measure"}, {"measure", "aggregation"}):
        return False
    if expression.get("kind", "measure") != "measure":
        return False
    if "aggregation" not in expression:
        return True
    measure = _by_id(getattr(config, "measures", None), expression["measure"])
    default = getattr(measure, "default_aggregation", None)
    return default is not None and expression["aggregation"] == default


def _clocks(config: Any, select: Any) -> set[str]:
    """Each selected subject's own clock, the one ``plan`` reads the question's window on."""

    from ..metadata import _compatible_temporal_roles_for_expr  # noqa: WPS433

    clocks: set[str] = set()
    for item in select if isinstance(select, list) else []:
        expression = item.get("expression") if isinstance(item, dict) else None
        if not isinstance(expression, dict):
            continue
        measure = _by_id(getattr(config, "measures", None), expression.get("measure"))
        recipe = _by_id(getattr(config, "metric_recipes", None), expression.get("metric"))
        if measure is not None:
            roles = list(getattr(measure, "compatible_temporal_roles", None) or [])
        elif recipe is not None and getattr(recipe, "temporal_role", None):
            roles = [recipe.temporal_role]
        elif recipe is not None:
            roles = _compatible_temporal_roles_for_expr(config, recipe.expression)
        else:
            continue
        clocks.update(str(role) for role in roles[:1])
    return clocks


def _drops_a_named_value(row: Any, named: dict[str, list[Any]]) -> bool:
    """Exactly ``{field, op: IS DISTINCT FROM, value}`` on a value an item names."""

    if not _plain_filter(row) or set(row) != {"field", "op", "value"}:
        return False
    read = _read_filter(row)
    return (
        read is not None
        and read[0] == _KEEPS_UNRECORDED
        and _contains_literal(named.get(row["field"], []), read[1][0])
    )


def _filter_entry(path: str, row: Any) -> dict[str, Any]:
    if not _plain_filter(row):
        return {"path": path, "value": row}
    return {
        "path": path,
        "field": row["field"],
        "op": row.get("op", "="),
        "value": row.get("value"),
    }


def _excess(
    config: Any, query: dict[str, Any], named: dict[str, list[Any]], window: dict[str, Any]
) -> list[dict[str, Any]]:
    """Every part of the draft outside a closed list, each with its path.

    Admitted: the inert keys and ``_`` annotations; ``group_by``, since the unasked-grouping
    check holds any grouping or grain the question doesn't trace to; one ``select`` item that
    is a plain reference; ``where`` rows that drop a value an item names; and a ``time`` block
    of the selected subject's own clock, a grain and the bounds of the question's ``window``.
    Every other key, value or node is excess by its key alone, a new key included.
    """

    out: list[dict[str, Any]] = []
    for key, value in query.items():
        if key in _INERT_KEYS or key == "group_by" or str(key).startswith("_"):
            continue
        if key == "select" and isinstance(value, list):
            out += [
                {"path": f"select[{index}]", "value": item}
                for index, item in enumerate(value)
                if index or not _plain_ref(config, item)
            ]
        elif key == "where" and isinstance(value, list):
            out += [
                _filter_entry(f"where[{index}]", row)
                for index, row in enumerate(value)
                if not _drops_a_named_value(row, named)
            ]
        elif key == "time" and isinstance(value, dict):
            clocks = _clocks(config, query.get("select"))
            out += [
                {"path": f"time.{part}", "value": item}
                for part, item in value.items()
                if not (
                    part == "grain"
                    or (part == "temporal_role" and item in clocks)
                    or (part in _BOUNDS and item == window.get(part))
                )
            ]
        else:
            out.append({"path": str(key), "value": value})
    return out


def exclusion_gaps(config: Any, text: str, query: dict[str, Any], window: Any) -> list[CoverageGap]:
    """The question's exclusion clauses that the draft doesn't realize item by item."""

    clauses = exclusion_clauses(config, text, window)
    if not clauses:
        return []
    return unrealized(
        clauses,
        query,
        config=config,
        window=dict(window.bounds),
        valid_values=_declared_values(config),
    )


def unrealized(
    clauses: list[ExclusionClause],
    query: dict[str, Any],
    *,
    config: Any = None,
    window: dict[str, Any] | None = None,
    valid_values: dict[str, list[Any]] | None = None,
) -> list[CoverageGap]:
    """One gap for each clause the draft doesn't realize item by item.

    Each value item needs a top-level ``IS DISTINCT FROM`` filter on its value and its one
    bound dimension. Beside an exclusion the draft, whoever supplied it (the caller's
    ``partial_query`` included), carries only what ``_excess`` admits: those filters, one
    plain measure or metric reference, the subject's own clock with the question's own time
    ``window``, a grain, grouping, ordering and inert context. Anything else is ``excess``.
    """

    if not clauses:
        return []
    where = list(query.get("where") or [])
    named: dict[str, list[Any]] = {}
    for clause in clauses:
        for item in clause.items:
            if item.binding is not None:
                named.setdefault(item.binding[0], []).append(item.binding[1])
    filters = [
        (f"where[{index}]", row, _read_filter(row))
        for index, row in enumerate(where)
        if _plain_filter(row)
    ]
    excess = _excess(config, query, named, window or {})
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
            if any(read is None for _path, _row, read in rows):
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
        # A filter rides on the clause naming its field; any other excess on the first one.
        extra = [
            row
            for row in excess
            if row.get("field") in fields or (number == 0 and row.get("field") not in named)
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
            else "The draft doesn't exclude exactly what the exclusion names, each value with "
            "a filter that keeps rows with no recorded value."
        ),
        expected=expected,
        actual={"where": where, **report, "excess": excess},
        recovery_hint={"kind": "provide_negative_filter", "message": " ".join(hints)},
    )


__all__ = [
    "ExcludedItem",
    "ExclusionClause",
    "excluded_time_spans",
    "exclusion_clauses",
    "exclusion_gaps",
    "exclusion_words",
    "exclusion_regions",
    "unrealized",
]
