"""Deterministic package-authoring checks for ambiguous semantic terms.

The detector is intentionally lexical and offline.  It flags terms that the
question planner normalizes to the same value, plus a deliberately narrow
one-edit typo case.  It does not try to infer semantic similarity from object
descriptions: doing that during ``validate-config`` would be noisy and would
make package validation dependent on a model or network service.
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

_COLLECTIONS: tuple[tuple[str, str], ...] = (
    ("entity", "entities"),
    ("dimension", "dimensions"),
    ("temporal_role", "temporal_roles"),
    ("relationship", "relationships"),
    ("measure", "measures"),
    ("metric", "metric_recipes"),
    ("segment", "segments"),
)

# Only objects competing for the same planner slot are compared.  A package
# commonly and legitimately has an entity, dimension, and measure all called
# "Order"; the typed query context separates those categories.  Measures and
# metrics share an analytic-value lookup domain, so that cross-kind pair is
# deliberately compared.
_LOOKUP_DOMAIN: dict[str, str] = {
    "entity": "entity",
    "dimension": "grouping",
    "temporal_role": "time_axis",
    "relationship": "relationship",
    "measure": "analytic_value",
    "metric": "analytic_value",
    "segment": "segment",
}

_FIELD_ORDER = {"label": 0, "alias": 1, "canonical_name": 2, "id": 3}


@dataclass(frozen=True)
class _Term:
    field: str
    value: str
    normalized: str
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class _ObjectTerms:
    object_id: str
    kind: str
    domain: str
    terms: tuple[_Term, ...]
    wrapped_measure_id: str = ""


def _tokens(value: Any) -> tuple[str, ...]:
    """Unicode-aware tokens with punctuation treated as a separator."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    separated = "".join(char if char.isalnum() else " " for char in text)
    return tuple(part for part in separated.split() if part)


def _term(field: str, value: Any) -> _Term | None:
    rendered = str(value or "").strip()
    tokens = _tokens(rendered)
    if not tokens:
        return None
    # The current planner's exact normalizer removes separators.  Match that
    # behavior so "gross-revenue" and "gross revenue" are reported as the
    # same lookup term, while retaining tokens for explanations and near-match
    # checks.
    return _Term(field=field, value=rendered, normalized="".join(tokens), tokens=tokens)


def _wrapped_measure_id(metric: Any) -> str:
    """Return the measure for a plain aggregate wrapper, otherwise ``""``."""
    expression = getattr(metric, "expression", None)
    if expression is None or type(expression).__name__ not in {"AggregateExpr", "MeasureRefExpr"}:
        return ""
    if getattr(expression, "filter", None) or getattr(expression, "window", None):
        return ""
    return str(getattr(expression, "measure", "") or "")


def _auto_metric_ids(config: Any) -> set[str]:
    """Best-effort IDs for generated measure mirrors.

    The compiled schema does not retain an explicit ``auto_published`` flag,
    and the package loader may inject an ID that differs from the later
    compiler default.  A plain wrapper that preserves both the measure's name
    and label is nevertheless an unactionable duplicate of one authored
    object.  Excluding that narrow shape avoids multiplying collision groups
    while keeping renamed curated wrappers in the check.
    """
    measure_by_id = {str(row.id): row for row in config.measures}
    out: set[str] = set()
    for metric in config.metric_recipes:
        measure = measure_by_id.get(_wrapped_measure_id(metric))
        if measure is None:
            continue
        metric_name = _term("canonical_name", getattr(metric, "name", ""))
        measure_name = _term("canonical_name", getattr(measure, "name", ""))
        metric_label = _term("label", getattr(metric, "label", ""))
        measure_label = _term("label", getattr(measure, "label", ""))
        if (
            metric_name is not None
            and measure_name is not None
            and metric_name.normalized == measure_name.normalized
            and metric_label is not None
            and measure_label is not None
            and metric_label.normalized == measure_label.normalized
        ):
            out.add(str(metric.id))
    return out


def _catalog_terms(config: Any) -> list[_ObjectTerms]:
    auto_metric_ids = _auto_metric_ids(config)
    objects: list[_ObjectTerms] = []
    for kind, collection_name in _COLLECTIONS:
        for obj in list(getattr(config, collection_name, []) or []):
            object_id = str(getattr(obj, "id", "") or "")
            if not object_id or (kind == "metric" and object_id in auto_metric_ids):
                continue
            # Primary-key dimensions are generated from entities and often
            # repeat their labels by construction.  They are not an authored
            # grouping-vocabulary choice.
            if (
                kind == "dimension"
                and str(getattr(obj, "semantic_kind", "") or "").casefold() == "id"
            ):
                continue
            values: list[tuple[str, Any]] = [
                ("id", object_id),
                ("canonical_name", getattr(obj, "name", "")),
                ("label", getattr(obj, "label", "")),
            ]
            values.extend(("alias", alias) for alias in list(getattr(obj, "aliases", []) or []))
            terms: list[_Term] = []
            seen: set[tuple[str, str]] = set()
            for field, value in values:
                candidate = _term(field, value)
                if candidate is None or (candidate.field, candidate.normalized) in seen:
                    continue
                seen.add((candidate.field, candidate.normalized))
                terms.append(candidate)
            objects.append(
                _ObjectTerms(
                    object_id=object_id,
                    kind=kind,
                    domain=_LOOKUP_DOMAIN[kind],
                    terms=tuple(terms),
                    wrapped_measure_id=_wrapped_measure_id(obj) if kind == "metric" else "",
                )
            )
    return sorted(objects, key=lambda row: (row.domain, row.kind, row.object_id))


def _is_intentional_wrapper_pair(left: _ObjectTerms, right: _ObjectTerms) -> bool:
    if left.kind == "metric" and right.kind == "measure":
        return left.wrapped_measure_id == right.object_id
    if right.kind == "metric" and left.kind == "measure":
        return right.wrapped_measure_id == left.object_id
    return False


def _components(rows: list[_ObjectTerms], edges: set[tuple[str, str]]) -> list[list[_ObjectTerms]]:
    by_id = {row.object_id: row for row in rows}
    adjacency: dict[str, set[str]] = defaultdict(set)
    for left, right in edges:
        adjacency[left].add(right)
        adjacency[right].add(left)
    components: list[list[_ObjectTerms]] = []
    pending = set(adjacency)
    while pending:
        root = min(pending)
        stack = [root]
        found: set[str] = set()
        while stack:
            current = stack.pop()
            if current in found:
                continue
            found.add(current)
            stack.extend(adjacency[current] - found)
        pending -= found
        components.append([by_id[object_id] for object_id in sorted(found)])
    return components


def _object_match_payload(row: _ObjectTerms, normalized: str) -> dict[str, Any]:
    matches = [term for term in row.terms if term.normalized == normalized]
    return {
        "id": row.object_id,
        "kind": row.kind,
        "matching_fields": sorted(
            {term.field for term in matches},
            key=lambda field: _FIELD_ORDER.get(field, len(_FIELD_ORDER)),
        ),
        "terms": sorted(
            {term.value for term in matches}, key=lambda value: (value.casefold(), value)
        ),
    }


def _recovery_hints(*, near: bool) -> list[str]:
    first = (
        "Correct the likely typo or make the two labels/synonyms more distinct."
        if near
        else "Give each object a distinct label or synonym for natural-language lookup."
    )
    return [
        first,
        "Keep shared business vocabulary in descriptions or topics instead of aliases.",
        "Use fully qualified object IDs when the overlap is intentional.",
    ]


def _exact_warnings(
    objects: list[_ObjectTerms], source_path: Path
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    index: dict[tuple[str, str], list[_ObjectTerms]] = defaultdict(list)
    for row in objects:
        for normalized in {term.normalized for term in row.terms}:
            index[(row.domain, normalized)].append(row)

    warnings: list[dict[str, Any]] = []
    exact_pairs: set[tuple[str, str]] = set()
    for (domain, normalized), rows in sorted(index.items()):
        unique = {row.object_id: row for row in rows}
        if len(unique) < 2:
            continue
        edges: set[tuple[str, str]] = set()
        for left, right in combinations(unique.values(), 2):
            if _is_intentional_wrapper_pair(left, right):
                continue
            left_id, right_id = sorted((left.object_id, right.object_id))
            pair = (left_id, right_id)
            edges.add(pair)
            exact_pairs.add(pair)
        for component in _components(list(unique.values()), edges):
            object_ids = [row.object_id for row in component]
            matched_values = sorted(
                {
                    term.value
                    for row in component
                    for term in row.terms
                    if term.normalized == normalized
                },
                key=lambda value: (value.casefold(), value),
            )
            displayed_term = matched_values[0]
            warnings.append(
                {
                    "code": "SEMANTIC_TERM_COLLISION",
                    "severity": "warning",
                    "message": (
                        f"{source_path}: semantic collision risk: term {displayed_term!r} "
                        f"normalizes identically for {', '.join(object_ids)}."
                    ),
                    "details": {
                        "collision_type": "exact_normalized",
                        "lookup_domain": domain,
                        "normalized_term": normalized,
                        "object_ids": object_ids,
                        "objects": [_object_match_payload(row, normalized) for row in component],
                        "risk": (
                            "Natural-language lookup can rank these objects indistinguishably."
                        ),
                        "recovery_hints": _recovery_hints(near=False),
                    },
                }
            )
    return warnings, exact_pairs


def _one_edit_apart(left: str, right: str) -> bool:
    """True for one insertion, deletion, substitution, or transposition."""
    if left == right or abs(len(left) - len(right)) > 1:
        return False
    if len(left) > len(right):
        left, right = right, left
    if len(left) == len(right):
        differences = [
            index for index, pair in enumerate(zip(left, right, strict=True)) if pair[0] != pair[1]
        ]
        if len(differences) == 1:
            return True
        return (
            len(differences) == 2
            and differences[1] == differences[0] + 1
            and left[differences[0]] == right[differences[1]]
            and left[differences[1]] == right[differences[0]]
        )
    mismatch = 0
    while mismatch < len(left) and left[mismatch] == right[mismatch]:
        mismatch += 1
    return left[mismatch:] == right[mismatch + 1 :]


def _near_match(left: _Term, right: _Term) -> tuple[str, str] | None:
    if left.normalized == right.normalized or len(left.tokens) != len(right.tokens):
        return None
    differences = [
        (left_token, right_token)
        for left_token, right_token in zip(left.tokens, right.tokens, strict=True)
        if left_token != right_token
    ]
    if len(differences) != 1:
        return None
    left_token, right_token = differences[0]
    # This narrow floor intentionally ignores short words ("net"/"new",
    # "day"/"days") and sibling qualifiers.  It targets likely typos, not
    # broad semantic relatedness.
    if min(len(left_token), len(right_token)) < 5:
        return None
    return (left_token, right_token) if _one_edit_apart(left_token, right_token) else None


def _near_warnings(
    objects: list[_ObjectTerms],
    source_path: Path,
    exact_pairs: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    # Index terms by every "one token removed" signature.  Only terms with
    # identical surrounding tokens can be a candidate, avoiding an O(N^2)
    # all-object comparison as catalogs grow.
    buckets: dict[tuple[str, int, int, tuple[str, ...]], list[tuple[_ObjectTerms, _Term]]] = (
        defaultdict(list)
    )
    for row in objects:
        for term in row.terms:
            if term.field == "id":
                continue
            for index, token in enumerate(term.tokens):
                if len(token) < 5:
                    continue
                context = (*term.tokens[:index], "*", *term.tokens[index + 1 :])
                buckets[(row.domain, len(term.tokens), index, context)].append((row, term))

    candidates_by_pair: dict[
        tuple[str, str], list[tuple[_ObjectTerms, _Term, _ObjectTerms, _Term, tuple[str, str]]]
    ] = defaultdict(list)
    for bucket in buckets.values():
        for (left, left_term), (right, right_term) in combinations(bucket, 2):
            if left.object_id == right.object_id or _is_intentional_wrapper_pair(left, right):
                continue
            left_id, right_id = sorted((left.object_id, right.object_id))
            pair = (left_id, right_id)
            if pair in exact_pairs:
                continue
            differing_tokens = _near_match(left_term, right_term)
            if differing_tokens is None:
                continue
            if left.object_id > right.object_id:
                left, right = right, left
                left_term, right_term = right_term, left_term
                differing_tokens = (differing_tokens[1], differing_tokens[0])
            candidates_by_pair[pair].append((left, left_term, right, right_term, differing_tokens))

    warnings: list[dict[str, Any]] = []
    for pair, candidates in sorted(candidates_by_pair.items()):
        left, left_term, right, right_term, differing_tokens = min(
            candidates,
            key=lambda item: (
                _FIELD_ORDER[item[1].field] + _FIELD_ORDER[item[3].field],
                len(item[1].normalized) + len(item[3].normalized),
                item[1].value.casefold(),
                item[3].value.casefold(),
            ),
        )
        object_ids = list(pair)
        warnings.append(
            {
                "code": "SEMANTIC_TERM_NEAR_COLLISION",
                "severity": "warning",
                "message": (
                    f"{source_path}: semantic collision risk: {left_term.value!r} and "
                    f"{right_term.value!r} differ by one likely typo across "
                    f"{left.object_id} and {right.object_id}."
                ),
                "details": {
                    "collision_type": "single_edit_typo",
                    "lookup_domain": left.domain,
                    "object_ids": object_ids,
                    "objects": [
                        {
                            "id": left.object_id,
                            "kind": left.kind,
                            "matching_field": left_term.field,
                            "term": left_term.value,
                        },
                        {
                            "id": right.object_id,
                            "kind": right.kind,
                            "matching_field": right_term.field,
                            "term": right_term.value,
                        },
                    ],
                    "differing_tokens": list(differing_tokens),
                    "edit_distance": 1,
                    "risk": (
                        "Typo-tolerant or semantic lookup can conflate these nearly "
                        "identical terms."
                    ),
                    "recovery_hints": _recovery_hints(near=True),
                },
            }
        )
    return warnings


def semantic_collision_warnings(config: Any, source_path: Path) -> list[dict[str, Any]]:
    """Return deterministic, structured warnings for ambiguous package terms."""
    objects = _catalog_terms(config)
    exact, exact_pairs = _exact_warnings(objects, source_path)
    near = _near_warnings(objects, source_path, exact_pairs)
    return sorted(
        [*exact, *near],
        key=lambda warning: (
            str(warning["code"]),
            tuple(warning["details"]["object_ids"]),
            str(warning["message"]),
        ),
    )
