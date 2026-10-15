"""Orchestrate intent patterns.

The orchestrator is intentionally thin: it iterates the ``PATTERNS``
registry, scores every matching draft, and returns the
highest-confidence match. Pattern priority is defined by registry order
when scores tie.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._base import RuntimeCompositionDraft, _runtime_composition_terms
from .intent_ir import IntentIR, parse_intent
from .patterns import PATTERNS
from .time_reference import with_time_reference


@dataclass(frozen=True)
class CompositionResult:
    """Output of ``compose`` — the IR plus (optionally) the realized
    draft and the name of the pattern that produced it.
    """

    intent_ir: IntentIR
    draft: RuntimeCompositionDraft | None
    pattern: str = ""


def _active_patterns(runtime: Any) -> list[Any]:
    """Filter ``PATTERNS`` for the calling entry point.

    ``package.planner.disabled_patterns`` lets packages opt out of
    individual patterns. Packages without a ``planner:`` block get the
    full registry. Unknown names in the disabled list are silently
    ignored.
    """

    package = getattr(getattr(runtime, "_config", None), "package", None)
    planner_cfg = getattr(package, "planner", None)
    disabled = set(getattr(planner_cfg, "disabled_patterns", None) or [])
    return [pattern for pattern in PATTERNS if pattern.name not in disabled]


def _select_draft(
    runtime: Any, text: str, terms: set[str]
) -> tuple[RuntimeCompositionDraft, str] | None:
    """Score-based pattern selection.

    Iterates every active pattern, collects the drafts that fire, and
    picks the highest-scoring one. Ties are broken by registry order
    (earlier patterns win). This replaces the legacy first-match-wins
    semantics with explicit scoring: catch-all patterns return a low
    score (~0.5) so they only win when no more specific pattern
    claims the intent, instead of re-implementing every earlier
    pattern's exclusion conditions to defer.

    For the common case (a single pattern fires) the orchestrator
    still picks that pattern. For the catch-all case the orchestrator
    naturally prefers the higher-scoring specific pattern. Returns
    ``None`` if no pattern fires.
    """

    fired = [
        (draft, pattern.name, index)
        for index, pattern in enumerate(_active_patterns(runtime))
        if (draft := pattern.match(runtime, text, terms)) is not None
    ]
    if not fired:
        return None
    # Higher score wins; on a score tie, the earlier registry index wins.
    draft, name, _index = max(fired, key=lambda row: (row[0].score, -row[2]))
    return draft, name


@with_time_reference
def compose(
    runtime: Any, intent: str, *, policy_context: dict[str, Any] | None = None
) -> CompositionResult:
    """Run the full composition pipeline.

    Parses the intent into an ``IntentIR``, then runs score-based
    pattern selection across the registry filtered by
    ``disabled_patterns``.
    Returns a ``CompositionResult`` with both the IR and the chosen
    draft — so the caller can fall back to IR-only ("unrealizable")
    behavior when no pattern fires.
    """

    intent_ir = parse_intent(runtime, intent, policy_context=policy_context)
    text = str(intent or "").lower()
    terms = _runtime_composition_terms(text)
    chosen = _select_draft(runtime, text, terms)
    if chosen is None:
        return CompositionResult(intent_ir=intent_ir, draft=None)
    draft, name = chosen
    return CompositionResult(intent_ir=intent_ir, draft=draft, pattern=name)


__all__ = ["CompositionResult", "compose"]
