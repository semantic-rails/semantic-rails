"""Catalog fallback ranking and refusal must not depend on randomized hashes."""

from __future__ import annotations

from typing import Any

import pytest

from semantic_rails.planner import generators
from semantic_rails.planner import plan as plan_module
from semantic_rails.planner.intent_ir import parse_intent
from semantic_rails.planner.orchestrator import CompositionResult


@pytest.mark.parametrize("hash_bonus", [0, 99])
@pytest.mark.parametrize("reverse", [False, True])
def test_equal_candidates_use_object_id_order(
    monkeypatch: pytest.MonkeyPatch, hash_bonus: int, reverse: bool
) -> None:
    rows = [
        {"id": object_id, "name": "orders", "label": "Orders", "score": 30.0}
        for object_id in ["measure.alpha.orders", "measure.beta.orders"]
    ]
    monkeypatch.setattr(
        generators,
        "hash",
        lambda object_id: hash_bonus if object_id == rows[0]["id"] else 99 - hash_bonus,
        raising=False,
    )
    ranked = generators._rerank_for_text(
        rows[::-1] if reverse else rows, runtime=None, intent="orders"
    )
    assert [row["id"] for row in ranked] == [row["id"] for row in rows]


@pytest.mark.parametrize("hash_bonus", [0, 99])
@pytest.mark.parametrize("detail", ["best", "full", "debug"])
def test_fallback_refusal_is_independent_of_hashes(
    runtime_factory: Any, monkeypatch: pytest.MonkeyPatch, hash_bonus: int, detail: str
) -> None:
    runtime = runtime_factory("jaffle_shop")
    delivered = "measure.jaffle.delivered_orders"
    monkeypatch.setattr(
        generators,
        "hash",
        lambda object_id: hash_bonus if object_id == delivered else 99 - hash_bonus,
        raising=False,
    )
    monkeypatch.setattr(
        plan_module,
        "compose",
        lambda runtime, intent: CompositionResult(
            intent_ir=parse_intent(runtime, intent), draft=None, pattern=""
        ),
    )
    try:
        payload = plan_module.plan_payload(
            runtime, intent="orders by store, customer type", detail=detail
        )
        # The fallback groups by the store it names and drops the customer type, whatever the
        # candidates' hashes.
        assert payload["status"] == "low_confidence"
        assert payload["why"]["code"] == "PLAN_UNMATCHED_TERMS"
        assert payload["why"]["details"]["dropped_groupings"] == ["customer type"]
        assert payload["best"]["query_ir"]["group_by"] == [
            "dimension.jaffle_store_id",
            "dimension.jaffle_store_name",
        ]
        assert "ready_for" not in payload["next"]
    finally:
        runtime.close()
