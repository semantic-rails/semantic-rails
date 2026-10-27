"""A select can't present a home-made calculation under a governed metric's or measure's name.

An inline count named `revenue_usd`, or the maximum of the revenue measure named `revenue_usd`,
would read as the governed revenue. Such a select is refused with the governed object it
impersonates and a name to use instead. The same calculation under a name of its own still
answers, and so does the governed object under its own name, however it is written.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import opened

REVENUE = "measure.jaffle.revenue_usd"
ORDERS = "measure.jaffle.order_count"
ITEM_REVENUE = "measure.jaffle.item_revenue_usd"
MONTHLY = {"time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"}}


@pytest.fixture(scope="module")
def runtime() -> Iterator[Runtime]:
    rt = Runtime("jaffle_shop")
    try:
        yield opened(rt)
    finally:
        rt.close()


def _select(expression: Any, alias: str | None = None) -> dict[str, Any]:
    item = expression if alias is None else {"expression": expression, "as": alias}
    return {"select": [item], **MONTHLY}


def _aggregate(measure: str, **extra: Any) -> dict[str, Any]:
    return {"kind": "aggregate", "measure": measure, **extra}


BEVERAGE = {
    "all": [{"field": "dimension.jaffle_item_product_type", "op": "is", "value": "beverage"}]
}


@pytest.mark.parametrize(
    ("expression", "alias", "governed", "suggested"),
    [
        # Order counts presented as revenue, by its name and by its id.
        (_aggregate(ORDERS), "revenue_usd", REVENUE, "order_count"),
        (_aggregate(ORDERS), REVENUE, REVENUE, "order_count"),
        # Raw-measure re-aggregation and another measure under revenue's name.
        (_aggregate(REVENUE, aggregation="max"), "revenue_usd", REVENUE, "max_revenue_usd"),
        (_aggregate(ITEM_REVENUE), "revenue_usd", REVENUE, "item_revenue_usd"),
        # A filtered revenue is not the governed revenue.
        (_aggregate(REVENUE, filter=BEVERAGE), "revenue_usd", REVENUE, "revenue_usd_calculated"),
        # A governed metric's own name, for another calculation.
        (_aggregate(REVENUE), "aov_usd", "metric.sales.aov_usd", "revenue_usd"),
    ],
    ids=["count-as-name", "count-as-id", "max", "other-measure", "filtered", "metric-name"],
)
def test_a_governed_name_on_another_calculation_is_refused(
    runtime, expression, alias, governed, suggested
) -> None:
    result = runtime.validate(_select(expression, alias))
    assert not result["ok"]
    (error,) = result["errors"]
    assert error["code"] == "OUTPUT_NAME_GOVERNED"
    assert error["details"] == {
        "path": "select[0].as",
        "alias": alias,
        "governed_object": governed,
        "governed_kind": governed.split(".", 1)[0],
        "suggested_alias": suggested,
    }
    assert f"'{alias}' is the name of the governed" in error["message"]
    assert f"such as '{suggested}'" in error["message"]
    # The suggested name answers.
    assert runtime.validate(_select(expression, suggested))["ok"]


@pytest.mark.parametrize(
    "query",
    [
        _select(REVENUE),
        _select(REVENUE, "revenue_usd"),
        _select(_aggregate(REVENUE), "revenue_usd"),
        _select(_aggregate(REVENUE, aggregation="sum"), "revenue_usd"),
        # Rounding changes precision, not what is counted.
        _select(
            {"kind": "call", "name": "ROUND", "args": [REVENUE, {"kind": "literal", "value": 2}]},
            "revenue_usd",
        ),
        # The same calculations under names of their own.
        _select(_aggregate(REVENUE, aggregation="max"), "max_revenue_usd"),
        _select(_aggregate(ORDERS), "orders_counted"),
        _select("metric.sales.aov_usd"),
        _select("metric.sales.aov_usd", "aov_usd"),
    ],
    ids=[
        "measure-default-name",
        "measure-own-name",
        "default-aggregate",
        "explicit-default-aggregation",
        "rounded",
        "max-own-name",
        "count-own-name",
        "metric-default-name",
        "metric-own-name",
    ],
)
def test_a_governed_object_or_a_name_of_its_own_still_answers(runtime, query) -> None:
    result = runtime.validate(query)
    assert result["ok"], result.get("errors")


def test_another_refusal_is_reported_first(runtime) -> None:
    """A select that fails for another reason (here an unknown metric) is refused for that
    reason, not as an impersonation."""
    result = runtime.validate(_select("metric.sales.revenu", "revenue_usd"))
    assert not result["ok"]
    assert result["errors"][0]["code"] != "OUTPUT_NAME_GOVERNED"


def test_a_refused_select_returns_no_number(runtime) -> None:
    with pytest.raises(SemanticLayerError) as refused:
        runtime.query(_select(_aggregate(ORDERS), "revenue_usd"))
    assert refused.value.code == "OUTPUT_NAME_GOVERNED"
