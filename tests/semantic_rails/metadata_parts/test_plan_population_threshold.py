"""Phase 3 — plan population-percentile rollups.

Reviewer's specific failing intents that motivated this round
(round-two #1 — "the biggest functional gap"): natural-language
phrases like "top decile of customers by lifetime spend" used to
fall through to keyword soup. After Phase 3 the same phrase resolves
to a structured IR with `scoped_aggregate` + `metric_predicate`
carrying a percentile threshold value.

The detector is an extension of the existing
``_qualified_metric_rollup`` path — same pattern dispatch, same
emitted shape, additional trigger phrases ("top decile of", etc.)
and an inline percentile threshold in place of the literal scalar.
Phase 2's parser change is what makes the dict value flow through.

The qualifier reads every row of each customer (``over: all_time``). Lifetime spend is a
balance, and a balance read through a predicate has no proven one-day read, so these drafts
validate but are held (``stock_as_of_unrealized``); a count qualifier answers.
"""

from __future__ import annotations

import duckdb
import pytest

from semantic_rails.planner import plan_payload
from tests.semantic_rails.planner_fixtures import jaffle_runtime

runtime = pytest.fixture(scope="module", name="runtime")(jaffle_runtime)


def _drafted_best(runtime, intent):
    """Return the best draft's call, asserting it validates but is held, not offered as ready."""
    payload = plan_payload(runtime, intent=intent, detail="full", limit=3)
    assert payload["status"] == "low_confidence", payload.get("why")
    assert "ready_for" not in payload["next"]
    assert payload["best"]["validation_ok"] is True
    kinds = {gap["kind"] for gap in payload["why"]["details"]["gaps"]}
    assert "stock_as_of_unrealized" in kinds
    return payload["best"]["query"]


@pytest.mark.parametrize(
    ("intent", "p"),
    [
        # Top decile = above the 90th percentile cut-off; "top 20%" the 80th.
        ("revenue from top decile of customers by lifetime spend", 0.9),
        ("revenue from top 20% of customers by lifetime spend", 0.8),
        ("orders from top quartile of customers by lifetime spend", 0.75),
    ],
)
def test_a_population_share_resolves_to_a_percentile_threshold(runtime, intent, p):
    (predicate,) = _drafted_best(runtime, intent)["select"][0]["expression"]["predicates"]
    assert predicate["entity"] == "entity.jaffle_customer"
    assert predicate["op"] == "gte"
    assert predicate["over"] == "all_time"
    assert predicate["value"] == {"kind": "percentile", "p": p}
    # The predicate reads the ranking measure ("lifetime spend") from the "by" phrase.
    assert predicate["input"] == "measure.jaffle.lifetime_spend_usd"


def test_percentile_intent_compiles_to_threshold_cte(runtime):
    """The drafted percentile call lowers to the threshold CTE."""
    query_ir = _drafted_best(runtime, "revenue from top decile of customers by lifetime spend")
    compile_result = runtime.compile(query_ir)
    assert compile_result["ok"], compile_result.get("errors")
    sql = compile_result["rendered_sql"].lower()
    assert "threshold" in sql
    assert "cross join" in sql
    assert "quantile_cont" in sql or "percentile_cont" in sql


def test_non_percentile_qualification_still_uses_literal_value(runtime):
    """The "with at least N" path keeps a literal threshold, on the target's own noun counted
    distinct, and answers: orders from customers with 4 or more orders."""
    plan = plan_payload(runtime, intent="orders from customers with at least 4 distinct orders")
    assert plan["status"] == "ok", plan.get("why")
    query = plan["best"]["query"]
    assert query["where"] == [
        {
            "entity": "entity.jaffle_customer",
            "having": [{"field": "measure.jaffle.order_count", "op": "gte", "value": 4}],
            "over": "all_time",
        }
    ]
    [row] = runtime.query(query)["rows"]
    with duckdb.connect(runtime.db_path, read_only=True) as connection:
        (reference,) = connection.execute(
            "SELECT COUNT(DISTINCT order_id) FROM jaffle_order WHERE customer_id IN "
            "(SELECT customer_id FROM jaffle_order GROUP BY 1 "
            "HAVING COUNT(DISTINCT order_id) >= 4)"
        ).fetchone()
    assert row["order_count"] == reference
    assert reference > 0
