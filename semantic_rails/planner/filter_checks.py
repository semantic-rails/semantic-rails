"""Faithfulness: filter values, where clauses, exclusions and contradictions."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..ast import is_child_group
from ._base import _singular, _tokens
from .coverage import (
    CoverageGap,
    _catalog_rows,
    _core_text,
    _dict_nodes,
    _plain,
    _referenced_ids,
    _value_phrases,
)
from .visibility import visible_dimensions

_EXCLUSION_VALUE_RE = (
    r"(?P<value>[^,.;]+?)(?=\s+(?:and\s+)?(?:excluding|except|without|but\s+not|not)\b|[,.;]|$)"
)
_NEGATION_RE = re.compile(
    r"\b(?P<marker>excluding|except|without|but\s+not|not)\s+"
    r"(?!only\b)" + _EXCLUSION_VALUE_RE,
    re.IGNORECASE,
)
# "for all stores but Brooklyn": "but" excludes after all/every/each/any.
_ALL_BUT_RE = re.compile(
    r"\b(?:all|every|each|any)\s+(?:[a-z-]+\s+){0,3}?(?P<marker>but)\s+(?!not\b)"
    + _EXCLUSION_VALUE_RE,
    re.IGNORECASE,
)


_NEGATIVE_OPS = frozenset(
    {
        "!=",
        "<>",
        "IS NOT",
        "IS NOT NULL",
        "NOT IN",
        "NOT LIKE",
        "NOT BETWEEN",
    }
)
# Filter ops that keep the values they name, and ops that drop them.
_KEEPING_OPS = frozenset({"=", "==", "IN"})
_EXCLUDING_OPS = frozenset({"!=", "<>", "NOT IN"})
# Everyday words that are also values in some catalogs ("new", "all", "other",
# "us", "open"). One names its value only next to a word of the value's
# dimension ("new customers"); otherwise the PLAN_UNMATCHED_TERMS warning,
# which alone never downgrades a plan, covers it; catalog-name words do downgrade it.
_EVERYDAY_WORDS = frozenset(
    {
        "active",
        "all",
        "any",
        "average",
        "best",
        "big",
        "closed",
        "current",
        "first",
        "good",
        "high",
        "inactive",
        "large",
        "last",
        "less",
        "low",
        "more",
        "new",
        "next",
        "no",
        "none",
        "normal",
        "old",
        "open",
        "other",
        "others",
        "same",
        "small",
        "standard",
        "top",
        "total",
        "us",
        "yes",
    }
)
# Identifier words too generic to tie an everyday word to a dimension.
_GENERIC_ID_WORDS = frozenset(
    {"code", "dimension", "flag", "has", "id", "is", "key", "name", "status", "type", "value"}
)


def _filter_value_gaps(runtime: Any, text: str, query: dict[str, Any]) -> list[CoverageGap]:
    """Every governed value the question names must reach the draft.

    A value is honored by a filter with the requested polarity, by
    grouping on its dimension when no filter drops it, or by a chosen object
    whose name carries the question's word for it ("new customer orders"
    answered by a new-customer measure). A filter that also keeps an unnamed
    value in an ungrouped total, or drops an unnamed value, is not. Longer
    values mask the words inside them ("New Orleans" is not "new"). Numbers,
    and everyday words not tied to their dimension in the question, are left
    to the unmatched-terms warning.
    """

    config = runtime._config
    phrases = _value_phrases(config)
    plain = _plain(text)
    matches = _value_matches(plain, phrases)
    if not matches:
        return []
    predicates = _field_predicates(query)
    # Every value the question names, in either polarity, by dimension. An
    # ungrouped total keeps only these, and an exclusion drops only these.
    named: dict[str, list[Any]] = {}
    for _span, phrase in matches:
        for domain, value in phrases[phrase]:
            for dimension in domain.dimensions:
                named.setdefault(str(dimension), []).append(value.value)
    # Keep punctuation and explicit inclusion transitions when assigning
    # polarity. _plain removes both, so its offsets cannot define a clause.
    source_words = list(re.finditer(r"[^\W_]+", text.lower()))
    excluded_spans = _excluded_value_spans(text)
    grouped = {str(item) for item in list(query.get("group_by") or [])}
    referenced = set(_referenced_ids(query))
    carried = {
        token
        for row in _catalog_rows(config)
        if str(getattr(row, "id", "")) in referenced
        for token in _tokens(_core_text(row))
    }
    said: list[str] = []
    missing: list[dict[str, Any]] = []
    for span, phrase in matches:
        rows = phrases[phrase]
        first_word = plain.count(" ", 0, span[0])
        last_word = first_word + plain[span[0] : span[1]].count(" ")
        original_span = (source_words[first_word].start(), source_words[last_word].end())
        negative = any(
            start <= original_span[0] and original_span[1] <= end for start, end in excluded_spans
        )
        if phrase in _EVERYDAY_WORDS and not _tied_to_dimension(config, plain, span, rows):
            continue
        if any(
            _value_honored(domain, value, predicates, grouped, negative, named)
            for domain, value in rows
        ):
            continue
        # The dedicated negation check already reports a missing negative
        # predicate or a positive predicate on this excluded value.
        if negative and (
            not _query_has_negative_semantics(query)
            or _positive_filter_evidence(runtime, query, phrase)
        ):
            continue
        relevant_filter = any(
            str(dimension) in predicates
            for domain, value in rows
            for dimension in domain.dimensions
        )
        if not negative and not relevant_filter and set(_tokens(phrase)) <= carried:
            continue
        said.append(phrase)
        for domain, value in rows:
            entry = next((row for row in missing if row["value"] == value.value), None)
            if entry is None:
                missing.append({"value": value.value, "dimensions": list(domain.dimensions)})
            else:
                entry["dimensions"].extend(
                    item for item in domain.dimensions if item not in entry["dimensions"]
                )
    if not missing:
        return []
    return [
        CoverageGap(
            kind="filter_values_unrealized",
            clause=", ".join(said),
            message="The draft does not preserve the requested inclusion or exclusion of named values.",
            expected={"values": missing},
            actual={
                "where": list(query.get("where") or []),
                "group_by": list(query.get("group_by") or []),
            },
            recovery_hint={
                "kind": "provide_filter_values",
                "message": (
                    "Use a filter with the requested polarity for every named value "
                    "(op 'in' for several included values of one dimension), then validate."
                ),
            },
        )
    ]


# "where channel is web" filters on a dimension it names, whether or not the
# catalog declares the value.
_WHERE_FIELD_RE = re.compile(
    r"\bwhere\s+(?:the\s+)?(?P<field>[a-z][a-z0-9 _-]*?)\s*"
    r"(?:\b(?:is|are|was|were|equals?)\b|[!=<>]=?)",
    re.IGNORECASE,
)


def _where_clause_gaps(runtime: Any, text: str, query: dict[str, Any]) -> list[CoverageGap]:
    """A "where <dimension> is <value>" clause needs a filter on that dimension."""

    predicates = _field_predicates(query)
    gaps: list[CoverageGap] = []
    for match in _WHERE_FIELD_RE.finditer(text):
        said = _singular(_plain(match.group("field")))
        fields = [
            str(row.id)
            for row in visible_dimensions(runtime._config)
            if said in {_singular(_plain(name)) for name in (row.label, *(row.aliases or []))}
        ]
        if fields and not any(field in predicates for field in fields):
            gaps.append(
                CoverageGap(
                    kind="dimension_filter_unrealized",
                    clause=match.group(0),
                    message="The question filters on a dimension it names, but the draft doesn't.",
                    expected={"filter_on": fields},
                    actual={"where": list(query.get("where") or [])},
                    recovery_hint={
                        "kind": "provide_dimension_filter",
                        "message": (
                            "Add a where filter on the named dimension, with a value from "
                            "valid_values, then validate."
                        ),
                    },
                )
            )
    return gaps


def _exclusion_matches(text: str) -> list[re.Match[str]]:
    return sorted(
        (match for pattern in (_NEGATION_RE, _ALL_BUT_RE) for match in pattern.finditer(text)),
        key=lambda match: match.start(),
    )


def _excluded_value_spans(text: str) -> list[tuple[int, int]]:
    """Negative clauses include comma lists, ending at an explicit inclusion."""

    spans: list[tuple[int, int]] = []
    matches = _exclusion_matches(text)
    for index, match in enumerate(matches):
        start = match.start("value")
        tail = text[start:]
        # In "not including Brooklyn", the first "including" completes
        # the exclusion; only a later one opens a positive clause.
        initial = re.match(r"(?:including|include)\b", tail, re.IGNORECASE)
        scan_from = initial.end() if initial else 0
        stop = re.search(r"[.;!?]|\b(?:including|include)\b", tail[scan_from:], re.IGNORECASE)
        end = start + scan_from + stop.start() if stop else len(text)
        if index + 1 < len(matches):
            end = min(end, matches[index + 1].start())
        spans.append((start, end))
    return spans


def _value_matches(
    plain: str, phrases: dict[str, list[tuple[Any, Any]]]
) -> list[tuple[tuple[int, int], str]]:
    """Value mentions in the question, longest first, never overlapping."""

    words = set(plain.split())
    taken: list[tuple[int, int]] = []
    found: list[tuple[tuple[int, int], str]] = []
    for phrase in sorted(phrases, key=len, reverse=True):
        parts = phrase.split()
        if not set(parts[:-1]) <= words or not {parts[-1], f"{parts[-1]}s", f"{parts[-1]}es"} & (
            words
        ):
            continue
        for match in re.finditer(rf"(?<!\S){re.escape(phrase)}(?:e?s)?(?!\S)", plain):
            span = match.span()
            if not any(span[0] < end and start < span[1] for start, end in taken):
                taken.append(span)
                found.append((span, phrase))
    return sorted(found)


def _tied_to_dimension(
    config: Any, plain: str, span: tuple[int, int], rows: list[tuple[Any, Any]]
) -> bool:
    """An everyday word sits next to a word of its value's dimension ("new customers")."""

    neighbors = plain[: span[0]].split()[-1:] + plain[span[1] :].split()[:1]
    near = {_singular(token) for token in _tokens(" ".join(neighbors))}
    dimensions = {str(item) for domain, _value in rows for item in domain.dimensions}
    generic = _GENERIC_ID_WORDS | _ubiquitous_words(config)
    words = {
        _singular(token)
        for row in visible_dimensions(config)
        if str(row.id) in dimensions
        for token in _tokens(_core_text(row))
    } - generic
    # A shared stem of four letters or more ties them too ("members", "membership").
    return any(
        word == other or (min(len(word), len(other)) >= 4 and word.startswith(other))
        for word in words
        for other in near
    )


def _ubiquitous_words(config: Any) -> set[str]:
    """Words in most dimension ids, such as a package prefix, which say nothing."""

    rows = visible_dimensions(config)
    counts = Counter(token for row in rows for token in set(_tokens(_core_text(row))))
    return {token for token, count in counts.items() if count * 2 > len(rows)}


@dataclass
class _FieldConstraints:
    """One query-level field's conjunctive predicates, in executable literals."""

    keeping: list[list[Any]] = field(default_factory=list)
    dropping: list[list[Any]] = field(default_factory=list)
    uncertain: bool = False

    def kept_literals(self) -> list[Any] | None:
        """Values admitted by every keeping predicate, if bounded."""

        if not self.keeping:
            return None
        candidates = list(self.keeping[0])
        for choices in self.keeping[1:]:
            candidates = [item for item in candidates if _contains_literal(choices, item)]
        return candidates

    def surviving_literals(self) -> list[Any] | None:
        """Values admitted by every known top-level predicate, if bounded."""

        kept = self.kept_literals()
        if kept is None:
            return None
        return [
            item
            for item in kept
            if not any(_contains_literal(choices, item) for choices in self.dropping)
        ]

    def keeps(self, canonical: Any, *, grouped: bool, named: list[Any] | None = None) -> bool:
        """The value survives. Given the question's ``named`` values, an ungrouped
        draft is one total, so it must keep no other value."""

        if self.uncertain:
            return False
        survivors = self.surviving_literals()
        if survivors is None:
            return grouped and not self.drops(canonical)
        return _contains_literal(survivors, canonical) and (
            named is None or grouped or all(_contains_literal(named, item) for item in survivors)
        )

    def drops(self, canonical: Any, *, named: list[Any] | None = None) -> bool:
        """The value is dropped. Given the question's ``named`` values, no other
        value that would otherwise survive is dropped too."""

        if self.uncertain or not any(
            _contains_literal(choices, canonical) for choices in self.dropping
        ):
            return False
        kept = self.kept_literals()
        return named is None or all(
            _contains_literal(named, item)
            or (kept is not None and not _contains_literal(kept, item))
            for choices in self.dropping
            for item in choices
        )


def _contains_literal(literals: list[Any], canonical: Any) -> bool:
    """SQL string equality has no catalog-label, case or punctuation rewrite."""

    return any(type(item) is type(canonical) and item == canonical for item in literals)


def _field_predicates(query: dict[str, Any]) -> dict[str, _FieldConstraints]:
    """Build query-level AND constraints; nested expression scopes remain unproven."""

    out: dict[str, _FieldConstraints] = {}
    for node in list(query.get("where") or []):
        if is_child_group(node):
            # A child group's conditions cut child rows, a scope of its own: unproven here.
            for condition in list(node.get("where") or []):
                if isinstance(condition, dict) and isinstance(condition.get("field"), str):
                    out.setdefault(condition["field"], _FieldConstraints()).uncertain = True
            continue
        if not isinstance(node, dict) or not isinstance(node.get("field"), str):
            continue
        name = node["field"]
        entry = out.setdefault(name, _FieldConstraints())
        op = " ".join(str(node.get("op") or "=").upper().split())
        literals = _membership_literals(op, node.get("value"))
        if literals is None:
            entry.uncertain = True
        elif op in _KEEPING_OPS:
            entry.keeping.append(literals)
        elif op in _EXCLUDING_OPS:
            entry.dropping.append(literals)
    # A selected expression can have its own where/predicate scope. Its rows
    # are not interchangeable with query-level where rows, so do not union or
    # intersect its values with the outer filter (or credit grouping through it).
    nested = {key: value for key, value in query.items() if key != "where"}
    for node in _dict_nodes(nested):
        name = node.get("field")
        if isinstance(name, str) and ("op" in node or "value" in node):
            out.setdefault(name, _FieldConstraints()).uncertain = True
    return out


def _membership_literals(op: str, raw: Any) -> list[Any] | None:
    """Only exact scalar comparisons and membership ops prove named-value polarity."""

    if op in {"IN", "NOT IN"}:
        literals = raw if isinstance(raw, list) else [raw]
    elif op in (_KEEPING_OPS | _EXCLUDING_OPS) and not isinstance(raw, (list, tuple, dict)):
        literals = [raw]
    else:
        return None
    if not literals or any(
        item is None or isinstance(item, (list, tuple, dict)) for item in literals
    ):
        return None
    return literals


def _value_honored(
    domain: Any,
    value: Any,
    predicates: dict[str, _FieldConstraints],
    grouped: set[str],
    negative: bool,
    named: dict[str, list[Any]],
) -> bool:
    """A filter has the requested polarity without keeping or dropping values the
    question doesn't name, or grouping keeps a positive value."""

    canonical = value.value
    for dimension in (str(item) for item in domain.dimensions):
        entry = predicates.get(dimension)
        names = named.get(dimension, [])
        if entry is not None:
            if negative and entry.drops(canonical, named=names):
                return True
            if not negative and entry.keeps(canonical, grouped=dimension in grouped, named=names):
                return True
        elif not negative and dimension in grouped:
            return True
    return False


def _contradictory_filter_gaps(query: dict[str, Any]) -> list[CoverageGap]:
    """Query-level conjunctions that leave no kept literal after exclusions."""

    conflicts: list[dict[str, Any]] = []
    for name, entry in _field_predicates(query).items():
        survivors = entry.surviving_literals()
        if survivors is None or survivors:
            continue
        conflicts.append(
            {
                "field": name,
                "values": sorted({str(item) for group in entry.keeping for item in group}),
            }
        )
    if not conflicts:
        return []
    return [
        CoverageGap(
            kind="contradictory_filters",
            clause="; ".join(
                f"{row['field']} = " + " and ".join(row["values"]) for row in conflicts
            ),
            message=(
                "The draft's filters on one field cannot retain any value together, so it "
                "returns no rows."
            ),
            expected={"filters": "one filter per field, op 'in' for several values"},
            actual={"conflicts": conflicts},
            recovery_hint={
                "kind": "merge_filter_values",
                "message": (
                    "Filter each dimension once, with op 'in' and a list for several values (or "
                    "group by it to compare them), then validate."
                ),
            },
        )
    ]


def _query_has_negative_semantics(query: dict[str, Any]) -> bool:
    for node in _dict_nodes(query):
        kind = str(node.get("kind", "") or "").casefold()
        if kind in {"not", "not_in", "not_between"} or node.get("negated") is True:
            return True
        if is_child_group(node) and node.get("match") == "none":
            return True
        op = " ".join(str(node.get("op", "") or "").upper().split())
        if op in _NEGATIVE_OPS:
            return True
        value = node.get("value")
        if value is False or (value == 0 and op in {"=", "<", "<="}):
            return True
    return False


def _positive_filter_evidence(
    runtime: Any, query: dict[str, Any], excluded_text: str
) -> list[dict[str, Any]]:
    """Positive outer filters that actually retain a canonical excluded value."""

    constraints = _field_predicates(query)
    phrases = _value_phrases(runtime._config)
    out: list[dict[str, Any]] = []
    for _span, phrase in _value_matches(_plain(excluded_text), phrases):
        for domain, value in phrases[phrase]:
            for dimension in (str(item) for item in domain.dimensions):
                entry = constraints.get(dimension)
                if entry is None or not entry.keeps(value.value, grouped=False):
                    continue
                for row in list(query.get("where") or []):
                    if not isinstance(row, dict) or row.get("field") != dimension:
                        continue
                    op = " ".join(str(row.get("op") or "=").upper().split())
                    literals = _membership_literals(op, row.get("value"))
                    if (
                        op in _KEEPING_OPS
                        and literals
                        and _contains_literal(literals, value.value)
                        and row not in out
                    ):
                        out.append(row)
    return out


def _negative_filter_evidence(runtime: Any, query: dict[str, Any], excluded_text: str) -> bool:
    """Every list item of the clause names a value an exact outer predicate drops,
    and no matched value is left undropped."""

    constraints = _field_predicates(query)
    phrases = _value_phrases(runtime._config)
    # The words of ``_plain``, kept with their raw positions so list marks survive.
    lowered = str(excluded_text or "").lower()
    tokens = list(re.finditer(r"[^\W_]+", lowered))
    starts = [0]
    for token in tokens:
        starts.append(starts[-1] + len(token.group()) + 1)
    matches = _value_matches(" ".join(token.group() for token in tokens), phrases)
    owner: list[int | None] = [None] * len(tokens)
    for number, ((start, end), _phrase) in enumerate(matches):
        for index in range(len(tokens)):
            if start <= starts[index] < end:
                owner[index] = number
    # A separator inside a matched value ("Food and Drink") does not split it.
    items: list[list[int | None]] = [[]]
    for index, token in enumerate(tokens):
        inside = index > 0 and owner[index] is not None and owner[index] == owner[index - 1]
        gap = lowered[tokens[index - 1].end() if index else 0 : token.start()]
        if not inside and re.search(r"[,;/&]", gap):
            items.append([])
        if owner[index] is None and token.group() in {"and", "or", "nor"}:
            items.append([])
        else:
            items[-1].append(owner[index])
    items = [item for item in items if item]
    return (
        bool(items)
        and all(any(number is not None for number in item) for item in items)
        and all(
            any(
                entry is not None and entry.drops(value.value)
                for domain, value in phrases[phrase]
                for dimension in domain.dimensions
                for entry in [constraints.get(str(dimension))]
            )
            for _span, phrase in matches
        )
    )
