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

    disabled: set[str] = set()
    package = getattr(getattr(runtime, "config", None), "package", None)
    if package is not None:
        planner_cfg = getattr(package, "planner", None)
        if planner_cfg is not None:
            disabled = set(getattr(planner_cfg, "disabled_patterns", None) or [])
    out = []
    for pattern in PATTERNS:
        if pattern.name in disabled:
            continue
        out.append(pattern)
    return out


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

    best: tuple[RuntimeCompositionDraft, str, int] | None = None
    for index, pattern in enumerate(_active_patterns(runtime)):
        draft = pattern.match(runtime, text, terms)
        if draft is None:
            continue
        if best is None:
            best = (draft, pattern.name, index)
            continue
        # Higher score wins; on score tie, earlier registry index wins.
        best_draft, _best_name, best_index = best
        if (draft.score, -index) > (best_draft.score, -best_index):
            best = (draft, pattern.name, index)
    if best is None:
        return None
    return best[0], best[1]


def compose(runtime: Any, intent: str) -> CompositionResult:
    """Run the full composition pipeline.

    Parses the intent into an ``IntentIR``, then runs score-based
    pattern selection across the registry filtered by
    ``disabled_patterns``.
    Returns a ``CompositionResult`` with both the IR and the chosen
    draft — so the caller can fall back to IR-only ("unrealizable")
    behavior when no pattern fires.
    """

    intent_ir = parse_intent(runtime, intent)
    text = str(intent or "").lower()
    terms = _runtime_composition_terms(text)
    chosen = _select_draft(runtime, text, terms)
    if chosen is None:
        return CompositionResult(intent_ir=intent_ir, draft=None)
    draft, name = chosen
    return CompositionResult(intent_ir=intent_ir, draft=draft, pattern=name)


__all__ = ["CompositionResult", "compose"]
