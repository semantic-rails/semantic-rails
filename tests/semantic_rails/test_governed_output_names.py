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


def test_a_stock_measure_read_as_its_sum_answers_under_its_own_name(runtime) -> None:
    """A stock read sums its members at the read day, so `sum` over a semi-additive measure is
    its default read, not another calculation."""
    stock = "measure.jaffle.inventory_on_hand_eop"
    daily = {"time": {"temporal_role": "temporal_role.jaffle_inventory_day", "grain": "month"}}
    for alias in ("inventory_on_hand_eop", stock):
        query = {
            "select": [{"expression": _aggregate(stock, aggregation="sum"), "as": alias}],
            **daily,
        }
        assert runtime.validate(query)["ok"], (alias, runtime.validate(query).get("errors"))


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


@pytest.mark.parametrize(
    ("hidden", "alias"),
    [
        # A governed measure, and a governed metric, the support caller can't see.
        ("measure.jaffle.median_item_revenue_usd", "median_item_revenue_usd"),
        ("metric.sales.aov_usd", "aov_usd"),
    ],
    ids=["hidden-measure", "hidden-metric"],
)
def test_a_hidden_object_s_name_answers_as_if_it_were_absent(tmp_path, hidden, alias) -> None:
    """The refusal reads the names the caller can see: a name only a hidden object holds is no
    governed name for that caller, and its answer is the absent package's, so the refusal never
    reveals that a hidden object exists."""
    from semantic_rails.config import load_package_config
    from semantic_rails.visible_view import hidden_object_ids
    from tests.semantic_rails.conftest import copy_package_config
    from tests.semantic_rails.hidden_absent import (
        CALLER,
        absent,
        envelope,
        visibility_policy,
        with_policies,
    )
    from tests.semantic_rails.visible_view_sweep import _public, mcp_as

    root = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    config = load_package_config(str(root))
    governed = with_policies(config, visibility_policy("hidden", hidden))
    hidden_ids = set(hidden_object_ids(governed, roles=["support"]))
    assert hidden in hidden_ids
    query = _select(_aggregate(ITEM_REVENUE, aggregation="max"), alias)
    arguments = {"mode": "validate", "query": query}
    seen = Runtime.from_config(governed, source_path=str(root))
    missing = Runtime.from_config(absent(config, hidden_ids), source_path=str(root))
    try:
        response = mcp_as(seen, "execute", arguments, CALLER)
        assert "OUTPUT_NAME_GOVERNED" not in str(response)
        expected = mcp_as(missing, "execute", arguments, CALLER)
        for answer in (response, expected):  # the request instant, not part of the answer
            answer.get("window", {}).pop("now", None)
        assert _public(envelope(response)) == _public(envelope(expected))
    finally:
        seen.close()
        missing.close()
    # A caller who can see the object is refused as before.
    visible = Runtime.from_config(config, source_path=str(root))
    try:
        assert (
            mcp_as(visible, "execute", arguments, CALLER)["errors"][0]["code"]
            == "OUTPUT_NAME_GOVERNED"
        )
    finally:
        visible.close()


def test_a_name_in_another_case_is_the_governed_name(runtime) -> None:
    """Most warehouses fold an unquoted identifier, so `Revenue_USD` names `revenue_usd`."""
    result = runtime.validate(_select(_aggregate(ORDERS), "Revenue_USD"))
    assert [error["code"] for error in result["errors"]] == ["OUTPUT_NAME_GOVERNED"]
    assert result["errors"][0]["details"]["governed_object"] == REVENUE


def test_an_authored_synonym_is_a_governed_name(tmp_path) -> None:
    """A metric's `synonyms:` are names it answers to: another calculation can't take one, in
    any case, and the metric itself answers under it."""
    import dataclasses

    from semantic_rails.config import load_package_config
    from semantic_rails.naming import governed_names, object_names, unclaimed_name
    from tests.semantic_rails.conftest import copy_package_config

    root = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    config = load_package_config(str(root))
    metrics = [
        dataclasses.replace(row, aliases=["Average_Ticket"])
        if row.id == "metric.sales.aov_usd"
        else row
        for row in config.metric_recipes
    ]
    config = dataclasses.replace(config, metric_recipes=metrics)
    aov = next(row for row in config.metric_recipes if row.id == "metric.sales.aov_usd")
    assert "average_ticket" in object_names(aov) and "average_ticket" in governed_names(config)
    assert unclaimed_name(["AVERAGE_TICKET", "ticket"], governed_names(config)) == "ticket"
    synonymous = Runtime.from_config(config, source_path=str(root))
    try:
        for alias in ("average_ticket", "AVERAGE_TICKET"):
            result = synonymous.validate(_select(_aggregate(ORDERS), alias))
            assert [error["code"] for error in result["errors"]] == ["OUTPUT_NAME_GOVERNED"]
            assert result["errors"][0]["details"]["governed_object"] == "metric.sales.aov_usd"
        assert synonymous.validate(_select("metric.sales.aov_usd", "average_ticket"))["ok"]
    finally:
        synonymous.close()
