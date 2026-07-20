"""Question classifier — routes intents to data_query or downstream chains.

:func:`classify_question` is the single gate every agent-facing
surface consults before composing a Query IR. Marked experimental: the
classifier is a small keyword-rule list (statistical_inference,
forecasting, workflow, diagnostic_synthesis, otherwise ``data_query``)
and is anchored to specific phrasings rather than bare tokens to avoid
false positives on legitimate catalog queries. Used by
:mod:`semantic_rails.runtime` (``validate``/``compile``/``query``),
``semantic_rails.planner.plan``, and :mod:`semantic_rails.metadata`
(``discover``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ScopeClassification:
    category: str
    confidence: float
    rationale: str
    what_the_sl_can_do_instead: str
    recommended_handoff: str

    def to_dict(self) -> dict:
        return asdict(self)


def classify_question(text: str) -> ScopeClassification:
    lowered = str(text or "").lower()
    # Rule needles are matched as substrings; keep them specific enough
    # that they don't false-positive on legitimate data intents. For
    # example, the bare token "email" matched valid catalog queries
    # like "sum email sms and push messages by region" — anchor the
    # workflow rule on "send to", "email to", "post to" instead, where
    # the intent is unambiguously a publication step.
    rules = [
        (
            "statistical_inference",
            (
                "statistically significant",
                "p-value",
                "p value",
                "control for",
                "feature importance",
                "regression",
            ),
            "Provide the underlying counts, rates, and cohorts that a stats workflow consumes.",
            "stats_chain",
        ),
        (
            "forecasting",
            (
                "forecast",
                "predict ",
                "predict.",
                "predict?",
                "project ",
                "assume ",
                "growth scenario",
            ),
            "Provide historical inputs and semantic definitions for a forecasting workflow.",
            "forecast_chain",
        ),
        (
            "workflow",
            (
                "publish to",
                "post to",
                "send to",
                "email to",
                "email the ",
                "build a sheet",
                "write to sheets",
            ),
            "Provide the governed data pull; a workflow tool should handle publication.",
            "workflow_tool",
        ),
        (
            "diagnostic_synthesis",
            (
                "why is",
                "why did",
                "why are",
                "what drove",
                "what caused",
                "explain the decline",
                "root cause",
            ),
            "Provide the component metrics and slices needed for diagnostic synthesis.",
            "analysis_chain",
        ),
    ]
    for category, needles, instead, handoff in rules:
        matched = [needle for needle in needles if needle in lowered]
        if matched:
            return ScopeClassification(
                category=category,
                confidence=0.82,
                rationale="matched " + ", ".join(matched[:3]),
                what_the_sl_can_do_instead=instead,
                recommended_handoff=handoff,
            )
    return ScopeClassification(
        category="data_query",
        confidence=0.72,
        rationale="no out-of-scope routing cue matched",
        what_the_sl_can_do_instead="Compile, validate, explain, or execute a governed semantic query.",
        recommended_handoff="semantic_rails",
    )
