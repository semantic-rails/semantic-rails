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

The planner drafts the lifetime qualifier on its own clock while the query is on the
order clock. The engine refuses matching the two by calendar bucket, so every draft here
is shown as ``low_confidence`` (never ready) and these tests assert the drafted IR and
the refusal code.
"""

from __future__ import annotations

import copy

from semantic_rails.planner import plan_payload

_REFUSED = "INVALID_TEMPORAL_BINDING"


def _drafted_best(runtime, intent):
    """Return the best draft's IR, asserting it is refused rather than offered as ready."""
    payload = plan_payload(runtime, intent=intent, detail="full", limit=3)
    assert payload["status"] == "low_confidence", payload.get("why")
    assert "ready_for" not in payload["next"]
    best = payload["best"]
    assert best["validation_ok"] is False
    report = runtime.validate(best["query_ir"])
    assert [error["code"] for error in report["errors"]] == [_REFUSED], report["errors"]
    return best["query_ir"]


def _predicates(query_ir):
    return query_ir["select"][0]["expression"]["predicates"]


def _aligned(query_ir):
    """The drafted query with every predicate opting into the query's calendar buckets."""
    aligned = copy.deepcopy(query_ir)
    for predicate in _predicates(aligned):
        predicate["time_alignment"] = "same_query_period"
    for outer in aligned.get("metric_filters", []):
        outer["expression"]["time_alignment"] = "same_query_period"
    return aligned


def test_top_decile_intent_resolves_to_percentile_threshold_ir(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        query_ir = _drafted_best(runtime, "revenue from top decile of customers by lifetime spend")
        (predicate,) = _predicates(query_ir)
        assert predicate["entity"] == "entity.jaffle_customer"
        assert predicate["op"] == ">="
        # Top decile = above the 90th percentile cut-off.
        assert predicate["value"] == {"kind": "percentile", "p": 0.9}
        # The predicate names the ranking measure ("lifetime spend") from the "by" phrase.
        assert predicate["measure"] == "measure.jaffle.lifetime_spend_before_tax_usd"
    finally:
        runtime.close()


def test_top_n_percent_intent_resolves_correct_p(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        # "top 20%" → p=0.8 (above the 80th percentile).
        query_ir = _drafted_best(runtime, "revenue from top 20% of customers by lifetime spend")
        (predicate,) = _predicates(query_ir)
        assert predicate["value"] == {"kind": "percentile", "p": 0.8}
    finally:
        runtime.close()


def test_top_quartile_intent_p_075(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        query_ir = _drafted_best(runtime, "orders from top quartile of customers by lifetime spend")
        (predicate,) = _predicates(query_ir)
        assert predicate["value"] == {"kind": "percentile", "p": 0.75}
    finally:
        runtime.close()


def test_percentile_intent_compiles_to_threshold_cte(runtime_factory):
    """The drafted percentile IR lowers to the threshold CTE once it asks for calendar alignment."""
    runtime = runtime_factory("jaffle_shop")
    try:
        query_ir = _drafted_best(runtime, "revenue from top decile of customers by lifetime spend")
        compile_result = runtime.compile(_aligned(query_ir))
        assert compile_result["ok"], compile_result.get("errors")
        sql = compile_result["rendered_sql"].lower()
        # Phase 2 lowering emits the threshold CTE + CROSS JOIN.
        assert "threshold" in sql
        assert "cross join" in sql
        assert "quantile_cont" in sql or "percentile_cont" in sql
    finally:
        runtime.close()


def test_non_percentile_qualification_still_uses_literal_value(runtime_factory):
    """Regression: the existing "with at least N" path (which produces
    a literal-scalar threshold) must keep working unchanged."""
    runtime = runtime_factory("jaffle_shop")
    try:
        query_ir = _drafted_best(runtime, "orders from customers with at least 4 distinct orders")
        (predicate,) = _predicates(query_ir)
        assert predicate["op"] == ">="
        assert predicate["value"] == 4
    finally:
        runtime.close()
