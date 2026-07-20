"""Pattern-based intent planning.

Each intent pattern lives in ``patterns/<name>.py`` and registers itself
in ``patterns.PATTERNS``. The orchestrator iterates the registry and
returns a realized draft. The only public agent-facing surface is
``plan_payload``; lower-level parser helpers are kept for internal tests,
debugging, and future planner work.

* ``IntentIR`` — typed parse of the natural-language intent.
* ``parse_intent`` — produce an ``IntentIR`` without realizing any
  pattern.
* ``compose`` — IR + draft + pattern name, in one call.
* ``compose_hints`` — small dict of the IR's resolved building blocks.
"""

from __future__ import annotations

from ._base import RuntimeCompositionDraft
from .intent_ir import IntentIR, ResolvedTerm, compose_hints, parse_intent
from .orchestrator import CompositionResult, compose
from .plan import plan_payload

__all__ = [
    "CompositionResult",
    "IntentIR",
    "ResolvedTerm",
    "RuntimeCompositionDraft",
    "compose",
    "compose_hints",
    "parse_intent",
    "plan_payload",
]
