"""Regression coverage for ``inline_period_shift`` on snapshot measures.

The pattern used to abort on any intent containing the word
"active" / "activity" because ``_threshold_from_text`` returns an
implicit ``(">", 0)`` for those tokens (the heuristic that treats
"active customer" as an activity qualification). For snapshot measures
like ``measure.jaffle.active_menu_count_eop`` the word "active" is
part of the measure name, not a qualification — so the period-shift
pattern should still fire.
"""

from __future__ import annotations

from semantic_rails.planner import plan_payload


def test_active_menu_yoy_fires_inline_period_shift(runtime_factory) -> None:
    """``active menu YoY by month`` must route to ``inline_period_shift`` (not fall through to
    the single-candidate path): a year-over-year comparison asks whether to read the years
    compared as one series, drafted as that series."""
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="active menu YoY by month")
    finally:
        runtime.close()
    assert payload["status"] == "needs_clarification", payload.get("why")
    assert payload["why"]["code"] == "PERIOD_COMPARISON"
    assert "execute" not in payload["next"].get("ready_for", [])
    best = payload["best"]
    assert best["pattern"] == "inline_period_shift"
    interpreted = best["interpreted_intent"]
    assert interpreted["pattern"] == "inline_period_shift"
    assert interpreted["measure"] == "measure.jaffle.active_menu_count_eop"
    assert interpreted["shift_grain"] == "year"
    assert best["query"]["time"]["last"] == {"unit": "year", "value": 2}
    assert best["query"]["time"]["grain"] == "month"
