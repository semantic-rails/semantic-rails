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

import pytest

from semantic_rails.planner.plan import plan_payload
from tests.semantic_rails.planner_fixtures import jaffle_runtime

runtime = pytest.fixture(scope="module", name="runtime")(jaffle_runtime)


@pytest.mark.parametrize(
    ("intent", "start", "end"),
    [
        ("revenue from Jan-Jun 2017 by month", "2017-01-01", "2017-07-01"),
        ("revenue from October 2017 through December 2017 by month", "2017-10-01", "2018-01-01"),
    ],
    ids=["shared-year", "year-rollover"],
)
def test_month_bounds_land_in_query_ir(runtime, intent, start, end):
    payload = plan_payload(runtime, intent=intent, detail="best")
    assert payload["status"] == "ok"
    time_spec = payload["best"]["query"]["time"]
    assert (time_spec.get("start"), time_spec.get("end")) == (start, end)


@pytest.mark.parametrize(
    ("intent", "code"),
    [
        # A funnel the draft doesn't realize is held with the curated conversion metrics.
        (
            "conversion rate of customers who ordered adele-ade and then ordered "
            "chai and mighty within 28 days, by month",
            "CONVERSION_INTENT_UNREALIZED",
        ),
        # Resolved to a conversion metric, it answers.
        ("session to order conversion rate (7d) by month", None),
        # The guard leaves other questions alone.
        ("monthly revenue by store", None),
        # Not a funnel, so the guard stays quiet; "converted" names catalog objects (Converted
        # Order Id) the revenue draft doesn't use: not ready.
        ("revenue converted to euros by month", "PLAN_UNMATCHED_TERMS"),
    ],
)
def test_only_an_unrealized_conversion_intent_is_held_for_it(runtime, intent, code):
    payload = plan_payload(runtime, intent=intent, detail="best")
    assert payload["status"] == ("low_confidence" if code else "ok"), payload.get("why")
    assert (payload.get("why") or {}).get("code") == code
    if code == "CONVERSION_INTENT_UNREALIZED":
        hint_kinds = {hint.get("kind") for hint in payload["why"]["recovery_hints"]}
        assert "author_conversion_ir" in hint_kinds
        assert payload["why"]["details"]["curated_conversion_metrics"]
    elif code == "PLAN_UNMATCHED_TERMS":
        assert payload["why"]["details"]["terms"] == ["converted"]
    elif "conversion" in intent:
        assert "conversion" in str(payload["best"]["query"]["select"])
