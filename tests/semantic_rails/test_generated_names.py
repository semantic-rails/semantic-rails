"""Columns the product generates are named after what they compute, never a governed name.

The build-options aggregation patch and the population-share plan draft rename a measure's
column when they compute something else from it. The new name is one no governed metric or
measure answers to (jaffle_shop governs `median_item_revenue_usd`, a daily-snapshot median), and
an order_by or having on the old column follows the rename, so every patch still validates.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from semantic_rails.metadata import build_options_payload
from semantic_rails.naming import governed_names, unclaimed_name
from semantic_rails.planner.call import Call, Condition, Order, QueryBuilder, Select
from semantic_rails.planner.patterns.qualified_metric_rollup import _qualified
from semantic_rails.query_call import read_call
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import opened

ITEM_REVENUE = "measure.jaffle.item_revenue_usd"
MONTHLY = {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"}


@pytest.fixture(scope="module")
def runtime() -> Iterator[Runtime]:
    rt = Runtime("jaffle_shop")
    try:
        yield opened(rt)
    finally:
        rt.close()


def _aggregation_patches(runtime: Runtime, call: dict[str, Any]) -> dict[str, Any]:
    payload = build_options_payload(
        runtime,
        partial_query=read_call(call, config=runtime.config)[0],
        focus_object_id=ITEM_REVENUE,
        step="aggregation",
        verbosity="full",
        limit=50,
    )
    rows = [
        row
        for key in ("recommended", "available")
        for row in payload.get(key, []) or []
        if row.get("object_type") == "aggregation"
    ]
    assert rows, payload
    return {row["id"]: row["query_patch"] for row in rows}


def _column(patch: dict[str, Any]) -> str:
    (item,) = patch["select"]
    return item if isinstance(item, str) else item["as"]


def test_unclaimed_name_skips_every_taken_name() -> None:
    assert unclaimed_name(["a", "b"], {"x"}) == "a"
    assert unclaimed_name(["a", "b"], {"a"}) == "b"
    assert unclaimed_name(["a", "b"], {"a", "b", "a_2"}) == "a_3"


def test_an_aggregation_patch_never_takes_a_governed_name(runtime) -> None:
    governed = governed_names(runtime.config)
    assert "median_item_revenue_usd" in governed  # a governed daily-snapshot median
    patches = _aggregation_patches(runtime, {"select": [ITEM_REVENUE], "time": MONTHLY})
    for aggregation, patch in patches.items():
        column = _column(patch)
        if column == ITEM_REVENUE:
            continue  # the measure's own default aggregate, under its own name
        assert column not in governed, (aggregation, column)
    assert _column(patches["median"]) == "item_revenue_usd_median"
    if "percentile" in patches:
        assert _column(patches["percentile"]) == "p50_item_revenue_usd"
    if "avg" in patches:
        assert _column(patches["avg"]) == "avg_item_revenue_usd"


@pytest.mark.parametrize("old", ["item_revenue_usd", ITEM_REVENUE, "revenue"])
def test_an_aggregation_patch_carries_its_order_and_having_along(runtime, old) -> None:
    select: Any = (
        {"expression": ITEM_REVENUE, "as": "revenue"} if old == "revenue" else ITEM_REVENUE
    )
    partial = {
        "select": [select],
        "group_by": ["dimension.jaffle_store_name"],
        "having": [{"field": old, "op": "gt", "value": 0}],
        "order_by": [{"field": old, "direction": "desc"}],
        "time": MONTHLY,
    }
    for aggregation, patch in _aggregation_patches(runtime, partial).items():
        column = _column(patch)
        expected = "item_revenue_usd" if column == ITEM_REVENUE else column
        assert [row["field"] for row in patch["order_by"]] == [expected], aggregation
        assert [row["field"] for row in patch["having"]] == [expected], aggregation
        result = runtime.validate(patch)
        assert result["ok"], (aggregation, result.get("errors"))


def test_a_population_share_draft_carries_its_order_and_having_along(runtime) -> None:
    config = runtime.config
    measure = "measure.jaffle.revenue_usd"
    base = Call(
        select=(Select(measure, "revenue_usd"),),
        group_by=("dimension.jaffle_store_name",),
        having=(Condition(measure, "gt", 0),),
        order_by=(Order("revenue_usd", "desc"),),
    )
    customer = next(row for row in config.entities if row.id == "entity.jaffle_customer")
    spend = next(row for row in config.measures if row.id == "measure.jaffle.lifetime_spend_usd")
    draft = _qualified(
        config,
        lambda: QueryBuilder(config, base),
        "all_time",
        customer,
        [spend],
        "gte",
        {"kind": "percentile", "p": 0.9},
    )
    (item,) = draft.select
    assert item.alias == "revenue_usd_qualifying"
    assert item.alias not in governed_names(config)
    assert [row.field for row in draft.order_by] == ["revenue_usd_qualifying"]
    assert [row.field for row in draft.having] == ["revenue_usd_qualifying"]
    result = runtime.validate(draft.to_call())
    assert result["ok"], result.get("errors")
