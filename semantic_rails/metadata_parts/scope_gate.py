"""Shared scope-gate payload helpers for metadata and planning surfaces."""

from __future__ import annotations

from typing import Any


def scope_block_payload(intent: str, classification: Any) -> dict[str, Any]:
    """Structured refusal block when the classifier rejects a request."""

    return {
        "code": "OUT_OF_SCOPE",
        "category": classification.category,
        "confidence": classification.confidence,
        "rationale": classification.rationale,
        "what_the_sl_can_do_instead": classification.what_the_sl_can_do_instead,
        "recommended_handoff": classification.recommended_handoff,
        "recovery_hint": (
            f"Hand the request off to the '{classification.recommended_handoff}' "
            "tool — the semantic layer compiles governed data queries, not "
            f"{classification.category.replace('_', ' ')} requests."
        ),
    }


__all__ = ["scope_block_payload"]
