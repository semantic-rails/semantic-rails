"""Discover ranking — surface a structured ``cross-entity`` reason on
dimension candidates whose root_entity disagrees with the entity the
query implies.

Handoff finding F1: ``revenue by store last 90 days`` used to return
``dimension.jaffle_inventory_date_day`` as the top dimension because
"store" + "day" both token-matched its fields, with no signal that
the dimension lived on a *different* entity than the top revenue
measure. The fix infers a consensus root entity from the top three
measure/metric candidates (when ≥2 agree) and attaches a structured
``cross-entity ...`` match_reason to dimensions on a different entity
— so blind agents reading ``dimensions[0]`` see the entity mismatch
even if the dimension's raw token score still surfaces it at top.

A small score penalty (-5) is also applied as a tiebreaker, but the
primary affordance is the structured reason. Heavier ranking surgery
would regress downstream planner paths that depend on raw
discover scores.
"""

from __future__ import annotations

from semantic_rails.metadata import discover_payload


def test_discover_emits_cross_entity_reason_for_revenue_by_store(runtime_factory) -> None:
    """The handoff repro: dimensions on the wrong entity must carry a
    `cross-entity` match_reason so blind agents reading the response
    can branch on it instead of trusting ``dimensions[0]`` blindly."""
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = discover_payload(runtime, terms="revenue by store last 90 days", limit=10)
        dimensions = payload.get("dimensions", []) or []
        assert dimensions, "expected at least one dimension candidate"
        inventory_row = next(
            (row for row in dimensions if row["id"] == "dimension.jaffle_inventory_date_day"),
            None,
        )
        assert inventory_row is not None, "expected inventory_date_day in candidate set"
        reasons = [str(r) for r in (inventory_row.get("match_reasons", []) or [])]
        assert any("cross-entity" in r for r in reasons), (
            f"inventory_date_day should carry a cross-entity reason for a revenue intent; "
            f"got reasons={reasons}"
        )
    finally:
        runtime.close()


def test_discover_no_cross_entity_reason_when_top_measures_disagree(runtime_factory) -> None:
    """When the top measures disagree on root_entity (the inference
    skips), no `cross-entity` reasons should fire — the penalty must
    only land when the query's entity is unambiguous."""
    runtime = runtime_factory("jaffle_shop")
    try:
        # An intent whose top measures span multiple entities — e.g.
        # mixing growth ratios (monthly_rollup), inventory measures
        # (inventory_snapshot), and order measures (order). The exact
        # mix depends on the catalog but we just verify the safety
        # net: when inference fails, no `cross-entity` reasons appear.
        payload = discover_payload(
            runtime, terms="top 3 stores by revenue in the most recent full month", limit=10
        )
        any_cross_entity = False
        for row in payload.get("dimensions", []) or []:
            reasons = [str(r) for r in (row.get("match_reasons", []) or [])]
            if any("cross-entity" in r for r in reasons):
                any_cross_entity = True
                break
        assert not any_cross_entity, (
            "expected no cross-entity reasons when top measures disagree on entity"
        )
    finally:
        runtime.close()
