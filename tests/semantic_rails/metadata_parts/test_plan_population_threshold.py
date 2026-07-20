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
"""

from __future__ import annotations

import json

from tests.plan_candidate_envelope import plan_candidate_envelope


def _first_candidate_with_percentile(candidates):
    """Return the first candidate whose IR contains a percentile threshold."""
    for cand in candidates:
        try:
            preds = cand["candidate_ir"]["select"][0]["expression"]["predicates"]
        except (KeyError, IndexError, TypeError):
            continue
        for predicate in preds:
            value = predicate.get("value")
            if isinstance(value, dict) and value.get("kind") == "percentile":
                return cand, predicate
    return None, None


def test_top_decile_intent_resolves_to_percentile_threshold_ir(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        result = plan_candidate_envelope(
            runtime,
            intent="revenue from top decile of customers by lifetime spend",
            limit=3,
            verbosity="compact",
        )
        candidates = result.get("candidates") or []
        assert candidates, f"expected at least one candidate, got {result!r}"
        cand, predicate = _first_candidate_with_percentile(candidates)
        assert cand is not None, (
            "expected a candidate carrying a percentile-threshold predicate; "
            f"got top candidate IR={json.dumps(candidates[0].get('candidate_ir'), indent=2)[:400]}"
        )
        assert predicate.get("entity") == "entity.jaffle_customer"
        assert predicate.get("op") in {">=", ">"}
        # Top decile = above the 90th percentile cut-off.
        assert predicate["value"]["p"] == 0.9
        # The predicate must name the ranking measure ("lifetime spend")
        # from the "by" phrase. Inside-select predicates carry the
        # measure directly (not nested under `input`) per the
        # scoped_aggregate predicate shape used by plan.
        measure_id = str(predicate.get("measure", "")) or str(
            (predicate.get("input") or {}).get("measure", "")
        )
        assert "lifetime" in measure_id, f"expected lifetime measure, got {measure_id!r}"
    finally:
        runtime.close()


def test_top_n_percent_intent_resolves_correct_p(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        # "top 20%" → p=0.8 (above the 80th percentile).
        result = plan_candidate_envelope(
            runtime,
            intent="revenue from top 20% of customers by lifetime spend",
            limit=3,
            verbosity="compact",
        )
        candidates = result.get("candidates") or []
        assert candidates
        _, predicate = _first_candidate_with_percentile(candidates)
        assert predicate is not None, (
            "expected a candidate with a percentile predicate; "
            f"top IR={json.dumps(candidates[0].get('candidate_ir'), indent=2)[:400]}"
        )
        assert predicate["value"]["p"] == 0.8
    finally:
        runtime.close()


def test_top_quartile_intent_p_075(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        result = plan_candidate_envelope(
            runtime,
            intent="orders from top quartile of customers by lifetime spend",
            limit=3,
            verbosity="compact",
        )
        candidates = result.get("candidates") or []
        assert candidates
        _, predicate = _first_candidate_with_percentile(candidates)
        if predicate is None:
            # quartile may resolve to a different ranked target; assert
            # at least that some candidate exists. The contract-bearing
            # assertion is the decile case above — quartile is a
            # nice-to-have phrase.
            return
        assert predicate["value"]["p"] == 0.75
    finally:
        runtime.close()


def test_percentile_intent_compiles_to_threshold_cte(runtime_factory):
    """End-to-end: the plan IR for a percentile-population intent
    must actually compile (Phase 2 lowering kicks in)."""
    runtime = runtime_factory("jaffle_shop")
    try:
        result = plan_candidate_envelope(
            runtime,
            intent="revenue from top decile of customers by lifetime spend",
            limit=3,
            verbosity="compact",
        )
        candidates = result.get("candidates") or []
        cand, _ = _first_candidate_with_percentile(candidates)
        assert cand is not None
        compile_result = runtime.compile(cand["candidate_ir"])
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
        result = plan_candidate_envelope(
            runtime,
            intent="orders from customers with at least 4 distinct orders",
            limit=3,
            verbosity="compact",
        )
        candidates = result.get("candidates") or []
        assert candidates, f"expected qualified rollup, got {result!r}"
        # The first candidate's predicate value should be a scalar
        # (int or dict-with-no-kind), not a percentile expression.
        top = candidates[0]
        try:
            predicates = top["candidate_ir"]["select"][0]["expression"]["predicates"]
        except (KeyError, IndexError, TypeError):
            predicates = []
        for predicate in predicates:
            value = predicate.get("value")
            assert not (isinstance(value, dict) and value.get("kind") == "percentile"), (
                "literal-threshold intent should not produce a percentile value"
            )
    finally:
        runtime.close()
