"""I5: ``inline_comparison`` pattern.

Catches "X vs Y" intents on the plan surface so the agent sees a
named pattern instead of empty fallback.
"""

from __future__ import annotations

import pytest

from semantic_rails.planner import compose

_FIRES_FROM_PROPOSE = [
    "food vs drink revenue",
    "revenue versus order count by store",
]


@pytest.mark.parametrize("intent", _FIRES_FROM_PROPOSE)
def test_inline_comparison_fires_from_plan(runtime_factory, intent: str) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        result = compose(runtime, intent)
    finally:
        runtime.close()
    assert result.pattern == "inline_comparison", (
        f"plan did not fire inline_comparison for {intent!r}; got pattern={result.pattern!r}"
    )
    interpreted = result.draft.interpreted_intent
    assert interpreted["left"]["id"]
    assert interpreted["right"]["id"]
    assert interpreted["left"]["id"] != interpreted["right"]["id"]
    assert len(result.draft.query["select"]) == 2


def test_inline_comparison_declines_non_vs_intents(runtime_factory) -> None:
    """An intent without 'vs' / 'versus' must not trigger this pattern."""

    runtime = runtime_factory("jaffle_shop")
    try:
        result = compose(runtime, "top stores by revenue")
    finally:
        runtime.close()
    assert result.pattern != "inline_comparison"
