"""Pattern: ``runtime_metric_arithmetic``.

Detects "total messages" intents (sum of sibling primitive metrics:
email + sms + push).
"""

from __future__ import annotations

from typing import Any

from .._base import (
    RuntimeCompositionDraft,
    _add_order,
    _maybe_group_by,
    _metric,
    _resolved,
)
from ._protocol import IntentPattern


def _match(runtime: Any, text: str, terms: set[str]) -> RuntimeCompositionDraft | None:
    config = runtime.config
    wants_messages = bool({"email", "sms", "push"} <= terms) and bool(
        {"total", "sum", "message"} & terms
    )
    if not wants_messages:
        return None

    metrics = [_metric(config, [term]) for term in ("email", "sms", "push")]
    if any(row is None for row in metrics):
        return None
    m0, m1, m2 = metrics[0], metrics[1], metrics[2]
    assert m0 is not None and m1 is not None and m2 is not None
    left = {
        "kind": "arithmetic",
        "op": "add",
        "left": {"metric": m0.id},
        "right": {"metric": m1.id},
    }
    query: dict[str, Any] = {
        "version": 2,
        "select": [
            {
                "as": "total_messages",
                "expression": {
                    "kind": "arithmetic",
                    "op": "add",
                    "left": left,
                    "right": {"metric": m2.id},
                },
            }
        ],
    }
    group_by = _maybe_group_by(config, text)
    if group_by:
        query["group_by"] = group_by
    _add_order(query)
    return RuntimeCompositionDraft(
        query=query,
        resolved=[_resolved(row) for row in metrics if row is not None],
        rationale=["composed runtime arithmetic over sibling primitive metrics"],
        interpreted_intent={"pattern": "runtime_metric_arithmetic", "source": "plan"},
    )


PATTERN = IntentPattern(name="runtime_metric_arithmetic", match=_match)
