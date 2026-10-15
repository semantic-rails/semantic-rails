"""Plan offers no draft on objects the question doesn't name, or on pieces that don't connect.

A held plan used to hand back a draft on the closest measure by any shared word ("How many
employees work at each store?" drafted the open store count), or the named pattern's draft that
fails validation beside a message about a fallback that validates. Now a question whose words
name no measure the draft could read gets no draft, with the words and the closest catalog
objects; a measure and a dimension that don't connect are named as the pair; and the drift
message says that best.query is the draft that fails. A plan that is ``ok`` never changes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from semantic_rails.planner import plan_payload
from semantic_rails.planner.call import Call
from semantic_rails.planner.plan_trace import _fallback_trace
from semantic_rails.runtime import Runtime
from tests.semantic_rails.planner_fixtures import jaffle_runtime

jaffle = pytest.fixture(scope="module", name="jaffle")(jaffle_runtime)
NOW = {"now": "2017-08-21T00:00:00Z"}
STORES = ["dimension.jaffle_store_id", "dimension.jaffle_store_name"]


def _plan(runtime: Runtime, question: str, detail: str = "best") -> dict[str, Any]:
    return plan_payload(
        runtime, intent=question, partial_query={"policy_context": NOW}, detail=detail
    )


def _no_draft(payload: dict[str, Any]) -> None:
    assert payload["best"] is None
    assert "ready_for" not in payload.get("next", {})
    assert not payload.get("alternatives")
    # No call anywhere in the answer: nothing to run by mistake.
    assert '"select"' not in json.dumps(payload)


@pytest.mark.parametrize("detail", ["best", "full", "query"])
@pytest.mark.parametrize(
    ("question", "terms"),
    [
        # The open store count shares only "store", which the grouping says.
        ("How many employees work at each store?", ["employees", "work"]),
        ("How many students enrolled in each course?", ["students", "enrolled"]),
        # Average order value shares only "average", which names no measure.
        ("average temperature by store", ["temperature"]),
    ],
)
def test_a_question_no_measure_matches_gets_no_draft(
    jaffle: Runtime, question: str, terms: list[str], detail: str
) -> None:
    payload = _plan(jaffle, question, detail)
    assert payload["status"] == "unrealizable", payload.get("why")
    why = payload["why"]
    assert why["code"] == "PLAN_SUBJECT_UNMATCHED"
    assert why["details"]["terms"] == terms
    # Nothing in the catalog mentions them, so there is nothing closer to offer.
    assert why["details"]["candidates"] == []
    assert ", ".join(terms) in why["message"]
    _no_draft(payload)


def test_the_closest_objects_are_offered_without_a_draft(jaffle: Runtime) -> None:
    # "threshold" is in the large orders' description, never in a name.
    payload = _plan(jaffle, "threshold breaches by store")
    assert payload["status"] == "low_confidence", payload.get("why")
    why = payload["why"]
    assert why["code"] == "PLAN_SUBJECT_UNMATCHED"
    assert why["details"]["terms"] == ["threshold", "breaches"]
    assert why["details"]["candidates"] == [
        {"id": "measure.jaffle.large_order_count", "label": "Large orders"}
    ]
    assert "Large orders (measure.jaffle.large_order_count)" in why["message"]
    assert payload["next"] == {"discover": {"terms": "threshold breaches"}}
    _no_draft(payload)


@pytest.mark.parametrize(
    "question",
    [
        # The subject is named; the unknown word holds the draft, which stays to inspect.
        "revenue by supplier",
        # No word but the ranked rows names a subject: the ranking hold says what's missing.
        "top 5 customers",
        # A subject named by any word the question asks about is named, even by an entity's
        # name: a written limitation, and the draft stays held.
        "customer satisfaction score by month",
    ],
)
def test_a_held_draft_on_a_named_subject_stays(jaffle: Runtime, question: str) -> None:
    payload = _plan(jaffle, question)
    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] != "PLAN_SUBJECT_UNMATCHED"
    assert payload["best"]["query"]["select"]


@pytest.mark.parametrize(
    ("question", "subject", "dimensions", "code"),
    [
        (
            "supply component cost by store",
            "measure.jaffle.supply_component_cost_usd",
            STORES,
            "MIXED_GRAIN_INVALID",
        ),
        (
            "revenue by supply name",
            "measure.jaffle.revenue_usd",
            ["dimension.jaffle_supply_name"],
            "MIXED_GRAIN_INVALID",
        ),
        (
            "orders by customer status",
            "measure.jaffle.order_count",
            ["dimension.jaffle_membership_status"],
            "PATH_NOT_FOUND",
        ),
        (
            "open store count by customer type",
            "measure.jaffle.open_store_count_eop",
            ["dimension.jaffle_customer_type"],
            "PATH_NOT_FOUND",
        ),
    ],
)
def test_pieces_that_dont_connect_are_named_without_a_draft(
    jaffle: Runtime, question: str, subject: str, dimensions: list[str], code: str
) -> None:
    payload = _plan(jaffle, question)
    # No draft is offered and the package can't answer as asked.
    assert payload["status"] == "unrealizable", payload.get("why")
    why = payload["why"]
    assert why["code"] == "PLAN_UNCONNECTED"
    assert why["details"]["subjects"] == [subject]
    assert why["details"]["dimensions"] == dimensions
    assert why["details"]["error"]["code"] == code
    for object_id in [subject, *dimensions]:
        assert object_id in why["message"]
    _no_draft(payload)


def test_a_drift_says_best_is_the_draft_that_fails(jaffle: Runtime) -> None:
    payload = _plan(jaffle, "session starts by product type")
    assert payload["status"] == "low_confidence"
    why = payload["why"]
    assert why["code"] == "PLAN_FALLBACK_SEMANTIC_DRIFT"
    assert payload["best"]["validation_ok"] is False
    assert why["message"].startswith("best.query is the named pattern's draft")
    assert "fails validation" in why["message"]
    assert [row["code"] for row in why["details"]["primary_errors"]] == ["AMBIGUOUS_PATH"]


def _row(select: str, pattern: str, ok: bool, **extra: Any) -> dict[str, Any]:
    from semantic_rails.planner._base import RuntimeCompositionDraft

    draft = RuntimeCompositionDraft(
        query=Call.from_call({"select": [select]}),
        resolved=[],
        rationale=[],
        interpreted_intent={"pattern": pattern},
    )
    return {"draft": draft, "pattern": pattern, "validation": {"ok": ok}, **extra}


def test_a_fallback_shown_says_what_it_is() -> None:
    primary = _row("measure.shop.revenue", "metric_by_dimension_rollup", ok=True)
    related = _row("measure.shop.item_revenue", "catalog_fallback", ok=True)
    unrelated = _row("measure.shop.store_count", "catalog_fallback", ok=True, placeholder=True)
    planned = [primary, related, unrelated]
    # The primary validated, so no fallback was checked against it.
    assert "preserved" not in _fallback_trace(related, planned)["reason"]
    trace = _fallback_trace(unrelated, planned)
    assert trace["placeholder"] is True
    assert trace["reason"].startswith("No measure or metric the question names matched")
    assert "don't run it" in trace["reason"]
    # A fallback checked against a primary that failed keeps saying so.
    failed = {**primary, "validation": {"ok": False}}
    assert "preserved" in _fallback_trace(related, [failed, related])["reason"]


def test_a_callers_own_grouping_keeps_its_validation_errors(jaffle: Runtime) -> None:
    # The caller wrote the grouping that can't be reached: the draft is their call, and the
    # engine's refusal is the why.
    status = "dimension.jaffle_membership_status"
    payload = plan_payload(
        jaffle,
        intent="orders by customer status",
        partial_query={"group_by": [status], "policy_context": NOW},
    )
    assert payload["status"] == "low_confidence"
    assert payload["why"]["code"] != "PLAN_UNCONNECTED"
    assert payload["best"]["query"]["group_by"] == [status]
    assert payload["best"]["validation_ok"] is False
