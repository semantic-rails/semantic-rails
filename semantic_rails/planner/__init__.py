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

Module map:

* ``_base`` — the draft type, tokens, synonyms and catalog matching.
* ``time_phrases`` — calendar and relative-window grammar, bounds and cues.
* ``time_windows`` — a question's time window, as-of cues and fiscal calendar.
* ``groupings`` — the groupings and time grain a question asks for.
* ``qualifiers`` — thresholds, qualifying phrases and entities, top-N requests.
* ``generators`` — the generic catalog fallback drafts.
* ``intent_ir`` — the typed parse of a question (``parse_intent``).
* ``orchestrator`` — run the pattern registry over the IR (``compose``).
* ``plan`` — ``plan_payload``: drafts, readiness holds and the payload.
* ``plan_query`` — the caller's partial query: merge, validate, trim errors.
* ``plan_trace`` — the best draft by semantic drift; trace and slim payloads.
* ``grouping_checks`` — groupings the question asks for that the draft dropped.
* ``unasked_groupings`` — groupings and grains the draft adds unasked.
* ``answer_shape`` — the rows, comparison and values the question asks for.
* ``intent_holds`` — time, currency and value words a draft doesn't carry.
* ``faithfulness`` — ``intent_faithfulness_why``; subject and metric checks.
* ``coverage`` — ``CoverageGap`` and the readers every check shares.
* ``time_checks`` — the draft's window and span match the question's.
* ``ranking_checks`` — rankings the question asks for.
* ``filter_checks`` — filter values, where clauses, exclusions, contradictions.
* ``consumed_spans`` — numerals, clock and zone words the draft must consume.
* ``unmatched_words`` — question words no part of the draft explains.
* ``time_reference`` — one caller clock for parsing, drafts and checks.
* ``visibility`` — caller-scoped visibility for every candidate path.
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
