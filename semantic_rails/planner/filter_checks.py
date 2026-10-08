"""Faithfulness: filter values, where clauses and contradictions."""

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
from .exclusions import exclusion_regions
from .visibility import visible_dimensions

# Filter ops that keep the values they name, and ops that drop them.
_KEEPING_OPS = frozenset({"=", "==", "IN"})
_EXCLUDING_OPS = frozenset({"!=", "<>", "NOT IN", "IS DISTINCT FROM"})
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
    """Every governed value the question names outside an exclusion must reach the draft.

    A value is honored by a filter that keeps it, by grouping on its dimension
    when no filter drops it, or by a chosen object whose name carries the
    question's word for it ("new customer orders" answered by a new-customer
    measure). A filter that also keeps an unnamed value in an ungrouped total
    is not. Longer values mask the words inside them ("New Orleans" is not
    "new"). Numbers, and everyday words not tied to their dimension in the
    question, are left to the unmatched-terms warning. ``exclusions`` owns
    every value an exclusion clause names, everyday words included.
    """

    config = runtime._config
    phrases = _value_phrases(config)
    plain = _plain(text)
    matches = _value_matches(plain, phrases)
    if not matches:
        return []
    predicates = _field_predicates(query)
    # Every value the question names, in either polarity, by dimension. An
    # ungrouped total keeps only these.
    named: dict[str, list[Any]] = {}
    for _span, phrase in matches:
        for domain, value in phrases[phrase]:
            for dimension in domain.dimensions:
                named.setdefault(str(dimension), []).append(value.value)
    # _plain removes punctuation, so its offsets cannot place a clause.
    source_words = list(re.finditer(r"[^\W_]+", text.lower()))
    excluded = exclusion_regions(text)
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
        if any(start <= original_span[0] < end for start, end in excluded):
            continue
        if phrase in _EVERYDAY_WORDS and not _tied_to_dimension(config, plain, span, rows):
            continue
        if any(_value_honored(domain, value, predicates, grouped, named) for domain, value in rows):
            continue
        relevant_filter = any(
            str(dimension) in predicates
            for domain, value in rows
            for dimension in domain.dimensions
        )
        if not relevant_filter and set(_tokens(phrase)) <= carried:
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

    def drops(self, canonical: Any) -> bool:
        """The value is dropped."""

        return not self.uncertain and any(
            _contains_literal(choices, canonical) for choices in self.dropping
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
    named: dict[str, list[Any]],
) -> bool:
    """A filter keeps the value without keeping values the question doesn't name,
    or grouping keeps it."""

    canonical = value.value
    for dimension in (str(item) for item in domain.dimensions):
        entry = predicates.get(dimension)
        if entry is not None:
            if entry.keeps(canonical, grouped=dimension in grouped, named=named.get(dimension, [])):
                return True
        elif dimension in grouped:
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
