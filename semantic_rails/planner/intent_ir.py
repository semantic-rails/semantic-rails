"""Intent IR — typed parse of a natural-language intent.

This module turns a raw intent string into a structured ``IntentIR``
the agent can consume directly. The IR is the contract that lets an
agent self-compose a Query IR when no built-in pattern realizes the
intent — instead of staring at an opaque "blocked" envelope, the agent
gets the *resolved building blocks* (legal measures, dimensions,
entities matching the user's terms) plus the parsed structural hints
(target, qualification, grain, top-N, comparison).

The parse is deterministic and side-effect-free. It reuses the
existing helpers from ``_base`` rather than reimplementing tokenization
or measure resolution. That guarantees the IR sees the same world the
patterns see.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ._base import (
    _maybe_group_by,
    _object_text,
    _period_shift_grain,
    _preferred_measure,
    _preferred_metric,
    _qualification_phrase,
    _qualification_phrase_token_groups,
    _qualifying_entity,
    _runtime_composition_terms,
    _target_measure_terms,
    _threshold_from_text,
    _time_spec,
    _top_n_intent,
)
from .generators import _target_focus_text


@dataclass(frozen=True)
class ResolvedTerm:
    """A catalog object that one of the intent's tokens resolved to.

    The agent can read ``id`` directly into a Query IR ``select``,
    ``group_by``, or ``where`` clause. ``score`` lets the agent
    detect close ties (the legacy ranking-fragility surface).
    """

    id: str
    object_type: str
    label: str
    score: float = 0.0


@dataclass(frozen=True)
class IntentIR:
    """Structured parse of a natural-language intent.

    Fields are deliberately flat and JSON-serializable so the IR can
    travel over MCP / HTTP unchanged.

    Population semantics:

    * ``subjects`` — measures or metrics the intent's tokens scored
      against, ranked by score. Empty if the intent doesn't mention a
      target.
    * ``grouping`` — dimensions the intent's "by X" / "per X" phrases
      resolved against (already de-duplicated and filtered).
    * ``time`` — the parsed time spec (grain + optional bounds) when
      the intent names one. Mirrors what the patterns would set.
    * ``qualifying_entity`` — the entity the qualification cohort is
      evaluated against ("customers with at least 4 orders" →
      ``entity.jaffle_customer``).
    * ``threshold`` — ``(op, value)`` tuple from a qualification
      phrase, or ``None``.
    * ``period_shift_grain`` — non-empty when the intent asks for a
      YoY/MoM/WoW/QoQ comparison.
    * ``comparators`` — left/right object IDs when the intent uses
      "X vs Y" phrasing.
    * ``unresolved`` — tokens with no catalog hit; surfaced so the
      agent can prompt the user for clarification.
    """

    intent: str
    terms: frozenset[str]
    # Resolved catalog objects (the agent can compose with these directly).
    subjects: tuple[ResolvedTerm, ...] = field(default_factory=tuple)
    grouping: tuple[ResolvedTerm, ...] = field(default_factory=tuple)
    qualifying_entity: ResolvedTerm | None = None
    # Structural hints.
    target_measure_terms: tuple[str, ...] = field(default_factory=tuple)
    qualification_phrase: str = ""
    qualification_token_groups: tuple[tuple[str, ...], ...] = field(default_factory=tuple)
    threshold: tuple[str, Any] | None = None
    time: dict[str, Any] | None = None
    period_shift_grain: str = ""
    is_top_intent: bool = False
    top_n: int = 5
    comparators: tuple[str, str] | None = None
    aggregate_hints: tuple[str, ...] = field(default_factory=tuple)
    unresolved: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for MCP / HTTP transport.

        Compact representation — empty fields are omitted so the
        default payload is small.
        """

        out: dict[str, Any] = {"intent": self.intent}
        if self.terms:
            out["terms"] = sorted(self.terms)
        if self.subjects:
            out["subjects"] = [_term_to_dict(term) for term in self.subjects]
        if self.grouping:
            out["grouping"] = [_term_to_dict(term) for term in self.grouping]
        if self.qualifying_entity is not None:
            out["qualifying_entity"] = _term_to_dict(self.qualifying_entity)
        if self.target_measure_terms:
            out["target_measure_terms"] = list(self.target_measure_terms)
        if self.qualification_phrase:
            out["qualification_phrase"] = self.qualification_phrase
        if self.qualification_token_groups:
            out["qualification_token_groups"] = [
                list(group) for group in self.qualification_token_groups
            ]
        if self.threshold is not None:
            op, value = self.threshold
            out["threshold"] = {"op": op, "value": value}
        if self.time:
            out["time"] = dict(self.time)
        if self.period_shift_grain:
            out["period_shift_grain"] = self.period_shift_grain
        if self.is_top_intent:
            out["is_top_intent"] = True
            out["top_n"] = self.top_n
        if self.comparators:
            out["comparators"] = {"left": self.comparators[0], "right": self.comparators[1]}
        if self.aggregate_hints:
            out["aggregate_hints"] = list(self.aggregate_hints)
        if self.unresolved:
            out["unresolved"] = list(self.unresolved)
        return out


def _term_to_dict(term: ResolvedTerm) -> dict[str, Any]:
    out: dict[str, Any] = {"id": term.id, "object_type": term.object_type, "label": term.label}
    if term.score:
        out["score"] = term.score
    return out


_COMPARISON_RE = re.compile(r"(.+?)\s+(?:vs|versus)\s+(.+)", re.IGNORECASE)

# Aggregation cues the agent may want to apply explicitly.
_AGGREGATE_HINT_PATTERNS = (
    (re.compile(r"\bp(\d{1,2})\b"), "percentile"),
    (re.compile(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*percentile\b", re.IGNORECASE), "percentile"),
    (re.compile(r"\baverage\b|\bavg\b|\bmean\b", re.IGNORECASE), "average"),
    (re.compile(r"\bmedian\b", re.IGNORECASE), "median"),
    (re.compile(r"\btotal\b|\bsum\b", re.IGNORECASE), "sum"),
    (re.compile(r"\bcount\b", re.IGNORECASE), "count"),
    (re.compile(r"\bshare\b|\bpct\b|\bpercent\b|%", re.IGNORECASE), "ratio"),
    (re.compile(r"\btop\s+\d+\b", re.IGNORECASE), "rank"),
)


def _aggregate_hints(text: str) -> tuple[str, ...]:
    hints: list[str] = []
    for pattern, label in _AGGREGATE_HINT_PATTERNS:
        if pattern.search(text) and label not in hints:
            hints.append(label)
    return tuple(hints)


# Minimum normalized subject score (0-1) below which a hit is treated
# as weak/spurious. Picked empirically: legitimate in-domain matches
# sit at ≥ 0.5 of the top hit; off-domain noise after stopword filtering
# either scores zero or shows up below this floor.
_MIN_SUBJECT_SCORE = 0.4


# Structural words that ``_base._score`` would match via substring
# (e.g. "the" is a substring of "the active menu count"). They never
# carry signal as a measure subject, so drop them before scoring.
# Time grains are filtered too — they describe HOW to slice, not WHAT
# the user wants as a measure.
_SUBJECT_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "between",
        "by",
        "compile",  # off-domain verbs that frequently substring-match
        "describe",
        "did",
        "do",
        "does",
        "each",
        "for",
        "from",
        "give",
        "had",
        "has",
        "have",
        "how",
        "in",
        "is",
        "less",
        "many",
        "more",
        "most",
        "much",
        "no",
        "not",
        "of",
        "on",
        "one",
        "or",
        "over",
        "per",
        "show",
        "some",
        "tell",
        "than",
        "that",
        "the",
        "these",
        "this",
        "those",
        "to",
        "under",
        "vs",
        "versus",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "without",
        # Time grains belong to ``time``, not ``subjects``.
        "day",
        "daily",
        "month",
        "monthly",
        "quarter",
        "quarterly",
        "week",
        "weekly",
        "year",
        "yearly",
    }
)


def _subject_candidates(
    config: Any, terms: set[str], *, focus_terms: set[str] | None = None
) -> list[ResolvedTerm]:
    """Rank measures + metrics that scored against ``terms``.

    Re-uses ``_score`` semantics from ``_base`` so the IR's view of
    "what the user mentioned" matches what the patterns see. Scores
    are normalized to [0, 1] by dividing by the top raw score; only
    the strongest hits stay in ``subjects``. Below-threshold rows are
    dropped so off-domain intents don't surface spurious top picks
    (the legitimately weak top-3 for "compile the linux kernel"
    becomes empty here).
    """

    from ._base import _score, _specificity_penalty  # local to avoid bloating module top.

    content_source = focus_terms or terms
    content_terms = {term for term in content_source if term not in _SUBJECT_STOPWORDS}
    if not content_terms:
        return []
    raw: list[tuple[int, str, str, str]] = []
    for row in config.measures:
        score = _score(row, content_terms) - _specificity_penalty(row, content_terms)
        if score <= 0:
            continue
        raw.append((score, getattr(row, "label", ""), getattr(row, "id", ""), "measure"))
    for row in config.metric_recipes:
        score = _score(row, content_terms) - _specificity_penalty(row, content_terms)
        if score <= 0:
            continue
        raw.append((score, getattr(row, "label", ""), getattr(row, "id", ""), "metric"))
    if not raw:
        return []
    top_score = max(item[0] for item in raw)
    # Sort by descending raw score, then label/id for stable ties.
    raw.sort(key=lambda item: (-item[0], item[1], item[2]))
    out: list[ResolvedTerm] = []
    for score, label, obj_id, object_type in raw[:8]:
        normalized = score / top_score if top_score else 0.0
        if normalized < _MIN_SUBJECT_SCORE:
            continue
        out.append(
            ResolvedTerm(
                id=obj_id,
                object_type=object_type,
                label=label,
                score=round(normalized, 3),
            )
        )
    return out


def _grouping_candidates(
    config: Any, text: str, target_terms: tuple[str, ...]
) -> list[ResolvedTerm]:
    dim_ids = _maybe_group_by(config, text, target_terms=target_terms)
    out: list[ResolvedTerm] = []
    for dim_id in dim_ids:
        dim = next((row for row in config.dimensions if row.id == dim_id), None)
        if dim is None:
            continue
        out.append(
            ResolvedTerm(
                id=getattr(dim, "id", ""),
                object_type="dimension",
                label=getattr(dim, "label", getattr(dim, "id", "")),
            )
        )
    return out


def _qualifying_entity_term(config: Any, terms: set[str], text: str) -> ResolvedTerm | None:
    entity = _qualifying_entity(config, terms, text=text)
    if entity is None:
        return None
    return ResolvedTerm(
        id=getattr(entity, "id", ""),
        object_type="entity",
        label=getattr(entity, "label", getattr(entity, "id", "")),
    )


def _unresolved_tokens(
    text: str,
    *,
    subjects: list[ResolvedTerm],
    grouping: list[ResolvedTerm],
    qualifying_entity: ResolvedTerm | None,
) -> list[str]:
    """Tokens that don't show up in any resolved object's text.

    This is a deliberately conservative signal — it surfaces obvious
    misses (the user typed a noun no catalog object knows) without
    flagging every stop word.
    """

    tokens = _runtime_composition_terms(text)
    # Approximate "covered" set: anything that appears as a token in
    # the resolved objects' searchable text. We reuse ``_object_text``
    # via a tiny shim because ResolvedTerm doesn't carry the raw row.
    covered: set[str] = set()
    for term in (*subjects, *grouping):
        covered.update(_runtime_composition_terms(term.label))
        covered.update(_runtime_composition_terms(term.id))
    if qualifying_entity is not None:
        covered.update(_runtime_composition_terms(qualifying_entity.label))
        covered.update(_runtime_composition_terms(qualifying_entity.id))
    # Stop tokens we already strip in qualification parsing.
    structural = {
        "of",
        "by",
        "for",
        "from",
        "in",
        "on",
        "the",
        "a",
        "an",
        "and",
        "or",
        "with",
        "without",
        "vs",
        "versus",
        "over",
        "under",
        "per",
        "each",
        "all",
        "any",
        "what",
        "which",
        "who",
        "that",
        "have",
        "had",
        "has",
        "give",
        "show",
        "tell",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "do",
        "does",
        "did",
        "this",
        "these",
        "those",
        "many",
        "much",
        "some",
        "more",
        "less",
        "than",
        "least",
        "most",
        "at",
        "to",
        "as",
        "between",
        "top",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        # time grains — already captured in ``time`` field
        "day",
        "week",
        "month",
        "quarter",
        "year",
        "monthly",
        "daily",
        "weekly",
        "quarterly",
        "yearly",
    }
    unresolved: list[str] = []
    for token in tokens:
        if token.isdigit():
            continue
        if token in structural:
            continue
        if token in covered:
            continue
        if token not in unresolved:
            unresolved.append(token)
    return unresolved


def parse_intent(runtime: Any, intent: str) -> IntentIR:
    """Parse ``intent`` into a structured ``IntentIR``.

    The function is read-only — it only reads catalog metadata. Safe to
    call before any of the workflow's compose / validate / compile
    steps.
    """

    text = str(intent or "").lower()
    config = getattr(runtime, "config", None)
    if config is None:
        return IntentIR(intent=intent, terms=frozenset(_runtime_composition_terms(text)))

    terms = _runtime_composition_terms(text)
    target_focus = _target_focus_text(text)
    target_focus_terms = _runtime_composition_terms(target_focus)
    target_terms = tuple(
        _target_measure_terms(target_focus, target_focus_terms)
        or _target_measure_terms(text, terms)
        or sorted(target_focus_terms)
    )
    qualification = _qualification_phrase(text)
    threshold = _threshold_from_text(text)
    shift_grain = _period_shift_grain(text)
    is_top, top_n = _top_n_intent(text)

    subjects = _subject_candidates(config, terms, focus_terms=target_focus_terms)
    grouping = _grouping_candidates(config, text, target_terms)
    qualifying_entity = _qualifying_entity_term(config, terms, text)

    # Time: pull a tentative time spec from the target object the planner
    # will prefer. This keeps metric intents such as conversion rate on
    # their metric clock instead of falling back to a weaker measure match.
    time_spec: dict[str, Any] | None = None
    if target_terms:
        metric = _preferred_metric(config, target_terms)
        role = str(getattr(metric, "temporal_role", "") or "") if metric else ""
        if not role:
            measure = _preferred_measure(config, target_terms)
            role = str(getattr(measure, "default_temporal_role", "") or "") if measure else ""
        if role:
            time_spec = _time_spec(role, text)
    if time_spec is None:
        for subject in subjects:
            if subject.object_type == "metric":
                row = next((m for m in config.metric_recipes if m.id == subject.id), None)
                role = str(getattr(row, "temporal_role", "") or "") if row else ""
            else:
                row = next((m for m in config.measures if m.id == subject.id), None)
                role = str(getattr(row, "default_temporal_role", "") or "") if row else ""
            if role:
                time_spec = _time_spec(role, text)
                break

    qualification_groups = tuple(
        tuple(group) for group in _qualification_phrase_token_groups(text, list(target_terms))
    )

    comparison_match = _COMPARISON_RE.search(text)
    comparators: tuple[str, str] | None = None
    if comparison_match:
        comparators = (
            comparison_match.group(1).strip(),
            comparison_match.group(2).strip(),
        )

    aggregate_hints = _aggregate_hints(text)
    unresolved = tuple(
        _unresolved_tokens(
            text,
            subjects=subjects,
            grouping=grouping,
            qualifying_entity=qualifying_entity,
        )
    )

    return IntentIR(
        intent=intent,
        terms=frozenset(terms),
        subjects=tuple(subjects),
        grouping=tuple(grouping),
        qualifying_entity=qualifying_entity,
        target_measure_terms=target_terms,
        qualification_phrase=qualification,
        qualification_token_groups=qualification_groups,
        threshold=threshold,
        time=time_spec,
        period_shift_grain=shift_grain,
        is_top_intent=is_top,
        top_n=top_n,
        comparators=comparators,
        aggregate_hints=aggregate_hints,
        unresolved=unresolved,
    )


# Helper retained for callers that just want the catalog "compose
# hints" without the full IR — useful for the ``unrealizable`` status
# branch of ``plan``.
def compose_hints(intent_ir: IntentIR) -> dict[str, Any]:
    """Return a small dict of the resolved building blocks for an agent
    that wants to self-compose a Query IR.

    Only the top few candidates per slot are included to keep the
    payload tight.
    """

    return {
        "subjects": [_term_to_dict(term) for term in intent_ir.subjects[:5]],
        "dimensions": [_term_to_dict(term) for term in intent_ir.grouping[:5]],
        "entity": (
            _term_to_dict(intent_ir.qualifying_entity)
            if intent_ir.qualifying_entity is not None
            else None
        ),
        "time": dict(intent_ir.time) if intent_ir.time else None,
        "threshold": (
            {"op": intent_ir.threshold[0], "value": intent_ir.threshold[1]}
            if intent_ir.threshold is not None
            else None
        ),
        "aggregate_hints": list(intent_ir.aggregate_hints),
    }


# Silence unused-import warnings — these are part of the public surface
# of this module so other code in the package can use them.
__all__ = [
    "IntentIR",
    "ResolvedTerm",
    "compose_hints",
    "parse_intent",
]

# Keep the helper imported so static analysis doesn't drop it; it's
# wired into _maybe_group_by paths above.
_ = _object_text
