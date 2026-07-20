"""Planner honesty for explicit date ranges and conversion intents.

External-agent feedback round three:

1. Natural-language historical ranges ("from January 2017 through June
   2017") were not preserved in the generated Query IR — the draft
   validated and silently answered an unbounded question.
2. The planner confidently returned AOV (status "ok") for "conversion
   rate of customers who ordered A and then ordered B within 28 days".

These tests pin the fixes: explicit month ranges land as
``time.start``/``time.end``, and conversion-shaped intents that realize
into non-conversion drafts come back ``low_confidence`` with a
structured ``why`` instead of a confident wrong answer.
"""

from __future__ import annotations

from semantic_rails.planner.plan import plan_payload


def _plan(runtime, intent: str, detail: str = "best") -> dict:
    return plan_payload(runtime, intent=intent, detail=detail)


def _best_time(payload: dict) -> dict:
    return ((payload.get("best") or {}).get("query_ir") or {}).get("time") or {}


def test_month_range_with_both_years_lands_in_query_ir(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    payload = _plan(runtime, "monthly revenue by store from January 2017 through June 2017")
    assert payload["status"] == "ok"
    time_spec = _best_time(payload)
    assert time_spec.get("start") == "2017-01-01"
    assert time_spec.get("end") == "2017-07-01"
    assert time_spec.get("grain") == "month"


def test_month_range_with_shared_year_lands_in_query_ir(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    payload = _plan(runtime, "revenue from Jan-Jun 2017 by month")
    assert payload["status"] == "ok"
    time_spec = _best_time(payload)
    assert time_spec.get("start") == "2017-01-01"
    assert time_spec.get("end") == "2017-07-01"


def test_single_month_with_year_lands_in_query_ir(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    payload = _plan(runtime, "revenue by store for March 2017")
    assert payload["status"] == "ok"
    time_spec = _best_time(payload)
    assert time_spec.get("start") == "2017-03-01"
    assert time_spec.get("end") == "2017-04-01"


def test_december_range_rolls_end_bound_into_next_year(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    payload = _plan(runtime, "revenue from October 2017 through December 2017 by month")
    time_spec = _best_time(payload)
    assert time_spec.get("start") == "2017-10-01"
    assert time_spec.get("end") == "2018-01-01"


def test_conversion_intent_with_non_conversion_draft_is_low_confidence(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    payload = _plan(
        runtime,
        "conversion rate of customers who ordered adele-ade and then ordered "
        "chai and mighty within 28 days, by month",
    )
    assert payload["status"] == "low_confidence"
    why = payload.get("why") or {}
    assert why.get("code") == "CONVERSION_INTENT_UNREALIZED"
    hint_kinds = {hint.get("kind") for hint in why.get("recovery_hints") or []}
    assert "author_conversion_ir" in hint_kinds
    assert why["details"]["curated_conversion_metrics"], (
        "expected curated conversion metrics to be surfaced for redirection"
    )


def test_conversion_intent_resolved_to_conversion_metric_stays_ok(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    payload = _plan(runtime, "session to order conversion rate by month")
    assert payload["status"] == "ok"
    select = (payload.get("best") or {}).get("query_ir", {}).get("select") or []
    assert "conversion" in str(select)


def test_non_conversion_intent_is_unaffected_by_the_guard(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    payload = _plan(runtime, "monthly revenue by store")
    assert payload["status"] == "ok"
    assert "why" not in payload


def test_currency_conversion_phrasing_does_not_trip_the_guard(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    payload = _plan(runtime, "revenue converted to euros by month")
    assert payload["status"] == "ok"
