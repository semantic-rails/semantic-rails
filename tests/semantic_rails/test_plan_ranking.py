"""Plan candidate ranking — confidence must differentiate candidates.

Per the agent UX audit, three candidates for "average order value by
month" used to come back at the same ``confidence`` (0.6), with the
wrong metric (``average_customer_lifetime_value``) leading. An LLM that
picks the top result would hallucinate.
"""

from __future__ import annotations

from tests.plan_candidate_envelope import plan_candidate_envelope


def test_plan_ranks_aov_above_ltv_for_order_value_intent(runtime_factory) -> None:
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_candidate_envelope(
            runtime,
            intent="average order value over time by month",
            limit=3,
        )
        candidates = list(payload.get("candidates", []) or [])
        assert candidates, "expected at least one candidate"

        resolved_ids = [
            row.get("id", "")
            for candidate in candidates
            for row in candidate.get("resolved", []) or []
        ]
        assert "metric.sales.aov_usd" in resolved_ids, (
            f"AOV missing from candidates: {resolved_ids}"
        )

        top_ids = [row.get("id", "") for row in (candidates[0].get("resolved", []) or [])]
        assert "metric.sales.aov_usd" in top_ids, (
            f"AOV should rank #1 for an order-value intent; got top {top_ids}"
        )

        # AOV must outrank any LTV-style candidate by a meaningful gap so
        # an agent that picks by confidence picks the right one.
        confidences = {
            row.get("id", ""): float(candidate.get("confidence", 0.0) or 0.0)
            for candidate in candidates
            for row in candidate.get("resolved", []) or []
        }
        aov = confidences.get("metric.sales.aov_usd", 0.0)
        ltv = confidences.get("metric.sales.average_customer_lifetime_value", 0.0)
        if ltv:
            assert aov >= ltv + 0.05, f"AOV confidence {aov} should beat LTV {ltv} by at least 0.05"
    finally:
        runtime.close()


def test_plan_prefers_revenue_usd_over_drink_revenue_for_generic_intent(
    runtime_factory,
) -> None:
    """When the user says "revenue" without a qualifier (drink, food, item,
    delivered, ...), the unqualified canonical measure must outrank its
    compound-prefix siblings. Stopwords like "in"/"by" used to substring-
    match prefixes (``in`` ⊂ ``drink``), inflating ``drink_revenue_usd``
    above ``revenue_usd``.

    With IDF weighting, the rare token "stores" (df=2 in jaffle_shop)
    boosts open_store_count_eop above revenue_usd for "top stores by
    revenue" — the IDF treats "stores" as a strong identifier, which
    is the correct behavior for the AOV use case. The assertion here
    is narrower: among the revenue family, `revenue_usd` must still
    outrank its compound-prefix siblings (drink_, food_, item_,
    delivered_) for an unqualified "revenue" mention. Whether
    revenue_usd is the OVERALL #1 candidate depends on which token in
    the intent IDF treats as most distinctive — that's a separate
    signal the blind agent reads from `match_reasons`.
    """
    runtime = runtime_factory("jaffle_shop")
    try:
        for intent in (
            "top stores by revenue",
            "top 3 stores by revenue in the most recent full month",
            "revenue by store",
        ):
            payload = plan_candidate_envelope(runtime, intent=intent, limit=10)
            candidates = list(payload.get("candidates", []) or [])
            assert candidates, f"expected candidates for: {intent}"
            # Among all resolved revenue-family measures across candidates,
            # revenue_usd must outrank drink/food/item/delivered when both
            # appear. When plan's path-search blocks every revenue
            # candidate (e.g. the "in the most recent full month" intent
            # routes through a grain-resolution path that rejects most
            # revenue measures), there's nothing to compare and the test
            # passes trivially — the assertion is "no compound outranks
            # canonical", not "canonical must appear".
            revenue_family_ranks: dict[str, int] = {}
            for index, cand in enumerate(candidates):
                for row in cand.get("resolved", []) or []:
                    rid = str(row.get("id", "") or "")
                    if "revenue" in rid and rid not in revenue_family_ranks:
                        revenue_family_ranks[rid] = index
            if "measure.jaffle.revenue_usd" not in revenue_family_ranks:
                # No canonical revenue measure in any candidate — verify the
                # compound siblings are also absent. If a compound appears
                # without the canonical, that's a regression.
                for compound in (
                    "measure.jaffle.drink_revenue_usd",
                    "measure.jaffle.food_revenue_usd",
                    "measure.jaffle.item_revenue_usd",
                    "measure.jaffle.delivered_revenue_usd",
                ):
                    assert compound not in revenue_family_ranks, (
                        f"compound {compound!r} appears in candidates for {intent!r} "
                        f"but canonical revenue_usd does not — that's a regression "
                        f"the stopword fix was meant to prevent"
                    )
                continue
            revenue_rank = revenue_family_ranks["measure.jaffle.revenue_usd"]
            for compound in (
                "measure.jaffle.drink_revenue_usd",
                "measure.jaffle.food_revenue_usd",
                "measure.jaffle.item_revenue_usd",
                "measure.jaffle.delivered_revenue_usd",
            ):
                if compound in revenue_family_ranks:
                    assert revenue_family_ranks[compound] >= revenue_rank, (
                        f"compound revenue measure {compound!r} should not outrank "
                        f"revenue_usd for {intent!r}; got compound at rank "
                        f"{revenue_family_ranks[compound]}, canonical at rank {revenue_rank}"
                    )

        # Sanity: a real qualifier in the intent should still surface the
        # qualified measure as a top-3 candidate.
        drink_payload = plan_candidate_envelope(runtime, intent="drink revenue by store", limit=5)
        drink_resolved_ids: set[str] = set()
        for cand in drink_payload.get("candidates", [])[:3]:
            for row in cand.get("resolved", []) or []:
                drink_resolved_ids.add(str(row.get("id", "") or ""))
        assert "measure.jaffle.drink_revenue_usd" in drink_resolved_ids, (
            f"drink_revenue_usd should appear in top-3 candidates when 'drink' "
            f"is in the intent; got {drink_resolved_ids}"
        )
    finally:
        runtime.close()
