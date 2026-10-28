"""A held plan whose question asks for a total carries a hint with the call shape that adds it;
plan never drafts a total, so its status and best.query are the question's own
(``planner/totals.py``)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import semantic_rails.planner.plan as plan_module
from semantic_rails.planner import plan_payload
from semantic_rails.planner.totals import _asked
from tests.semantic_rails.planner_fixtures import jaffle_runtime

runtime = pytest.fixture(scope="module", name="runtime")(jaffle_runtime)

STORES = ["dimension.jaffle_store_id", "dimension.jaffle_store_name"]
NOW = "2018-01-01T00:00:00Z"


def _plan(runtime: Any, question: str) -> dict[str, Any]:
    return plan_payload(runtime, intent=question, detail="query", now=NOW)


def _hints(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in plan.get("warnings", []) if row["code"] == "PLAN_TOTALS_HINT"]


def _without_hints(plan: dict[str, Any]) -> dict[str, Any]:
    out = dict(plan)
    warnings = [row for row in plan.get("warnings", []) if row["code"] != "PLAN_TOTALS_HINT"]
    if warnings:
        out["warnings"] = warnings
    else:
        out.pop("warnings", None)
    return out


@pytest.mark.parametrize(
    "question,kind,add",
    [
        ("revenue by store with the grand total", "rows", {"totals": []}),
        ("revenue by store with a total row", "rows", {"totals": []}),
        (
            "revenue by store and customer type with subtotals by store",
            "subtotals",
            # A level keeps the key of each name it keeps: the store's name alone.
            {"totals": ["dimension.jaffle_store_name"]},
        ),
        (
            "order count by store and month with each store's total across months",
            "beside",
            {
                "select": [
                    {
                        "expression": {
                            "kind": "total",
                            "input": "measure.jaffle.order_count",
                            "over": ["period"],
                        },
                        "as": "order_count_total",
                    }
                ]
            },
        ),
    ],
)
def test_a_held_plan_hints_the_total_its_question_asks_for(runtime, question, kind, add):
    plan = _plan(runtime, question)
    assert plan["status"] != "ok"
    [hint] = _hints(plan)
    assert hint["severity"] == "info"
    assert hint["details"]["kind"] == kind
    assert hint["details"]["add"] == add
    assert "plan doesn't draft" in hint["message"]


def test_a_share_hint_names_the_draft_s_value_and_grouping(runtime):
    plan = _plan(runtime, "each store's share of revenue")
    [hint] = _hints(plan)
    [item] = hint["details"]["add"]["select"]
    assert item["expression"]["kind"] == "share"
    assert item["expression"]["over"] == STORES
    assert item["expression"]["input"] in plan["best"]["query"]["select"]


@pytest.mark.parametrize(
    "question",
    [
        "revenue by store",
        "total revenue by store",
        # The package's own share metric answers it.
        "food revenue as a share of total revenue by store",
        "what share of total revenue was food revenue by store",
    ],
)
def test_a_ready_plan_carries_no_hint(runtime, question):
    plan = _plan(runtime, question)
    assert plan["status"] == "ok"
    assert not _hints(plan)


@pytest.mark.parametrize(
    "question",
    [
        "revenue by store with the grand total",
        "revenue by store with the grand total and number of orders",
        "each store's share of overall revenue for new customers",
        "revenue by store and customer type with subtotals by store",
        "each month's share of revenue",
    ],
)
def test_the_hint_leaves_the_plan_as_it_is(runtime, monkeypatch, question):
    """Status, why and best.query are the question's own: the hint is the only difference."""
    with_hint = _plan(runtime, question)
    monkeypatch.setattr(plan_module, "with_totals_hint", lambda config, payload, text: payload)
    assert _without_hints(with_hint) == _plan(runtime, question)


@pytest.mark.parametrize(
    "question",
    [
        "total revenue by store",
        "revenue and total orders by store",
        "revenue by store with total revenue",
        "food revenue share by store",
        "food revenue as a share of total revenue by store",
        "each store's percentage of revenue",
        "each store's share of food revenue",
    ],
)
def test_a_total_that_is_a_measure_s_own_word_gets_no_hint(runtime, question):
    assert _asked(question, runtime._config) is None


def test_a_catalog_name_holding_a_total_s_word_is_read_as_that_object():
    order_total = SimpleNamespace(label="Order total", name="order_total", aliases=[])
    config = SimpleNamespace(measures=[order_total], metric_recipes=[], dimensions=[], segments=[])
    assert _asked("order total for each store", config) is None
    asked = _asked("revenue by store with the grand total", config)
    assert asked is not None and asked[0] == "rows"
