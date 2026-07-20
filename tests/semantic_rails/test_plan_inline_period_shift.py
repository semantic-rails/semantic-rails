"""Regression coverage for ``inline_period_shift`` on snapshot measures.

Before A20 the pattern aborted on any intent containing the word
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
    """``active menu YoY by month`` must route to ``inline_period_shift``
    and produce a 2-column current/prior IR — not fall through to the
    legacy single-candidate path (status='ok', pattern='').
    """
    runtime = runtime_factory("jaffle_shop")
    try:
        payload = plan_payload(runtime, intent="active menu YoY by month")
    finally:
        runtime.close()
    assert payload["status"] == "ok"
    best = payload["best"]
    assert best["pattern"] == "inline_period_shift"
    interpreted = best["interpreted_intent"]
    assert interpreted["pattern"] == "inline_period_shift"
    assert interpreted["measure"] == "measure.jaffle.active_menu_count_eop"
    assert interpreted["shift_grain"] == "year"
    # The realized IR should carry both the current-period select and
    # the prior_period shorthand.
    select = best["query_ir"]["select"]
    assert len(select) == 2
    assert any(col.get("expression", {}).get("kind") == "prior_period" for col in select)
