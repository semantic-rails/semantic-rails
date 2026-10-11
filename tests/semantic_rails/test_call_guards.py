"""Guards on what a call asks for: column names, statistic overrides, governed measures,
shares of a total, rank by key."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.query_call import read_call
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig

REVENUE = "measure.jaffle.revenue_usd"
STORE = "dimension.jaffle_store_name"


@pytest.fixture()
def runtime(runtime_factory: Any) -> Iterator[Runtime]:
    rt = runtime_factory("jaffle_shop")
    try:
        yield rt
    finally:
        rt.close()


def _refusal(call: dict[str, Any]) -> SemanticLayerError:
    with pytest.raises(SemanticLayerError) as exc:
        read_call(call)
    return exc.value


def test_a_reference_is_named_by_its_last_segment_and_a_kind_by_the_engine() -> None:
    select = read_call(
        {
            "select": [
                REVENUE,
                {"expression": REVENUE, "as": "total"},
                {"expression": {"kind": "measure", "measure": REVENUE, "aggregation": "avg"}},
            ]
        }
    )[0]["select"]
    assert [row["as"] for row in select] == ["revenue_usd", "total", "expr_3"]


@pytest.mark.parametrize(
    ("call", "path", "fix"),
    [
        (
            {"select": [REVENUE, {"expression": REVENUE}]},
            "select[1]",
            '{"expression": "measure.jaffle.revenue_usd", "as": "revenue_usd_2"}',
        ),
        (
            {"select": [{"expression": REVENUE, "as": "period"}], "time": {"grain": "month"}},
            "select[0]",
            '"as": "period_2"',
        ),
        (
            {"select": [{"expression": REVENUE, "as": STORE}], "group_by": [STORE]},
            "select[0]",
            f'"as": "{STORE}_2"',
        ),
        ({"select": [REVENUE], "group_by": [STORE, STORE]}, "group_by[1]", "Remove group_by[1]"),
    ],
)
def test_a_repeated_column_name_is_refused_with_the_as_that_fixes_it(
    call: dict[str, Any], path: str, fix: str
) -> None:
    error = _refusal(call)
    assert error.code == "DUPLICATE_OUTPUT_ALIAS"
    assert error.details["path"] == path
    [hint] = error.details["recovery_hints"]
    assert fix in hint["message"]


def test_period_is_a_free_name_without_a_grain() -> None:
    select = read_call({"select": [{"expression": REVENUE, "as": "period"}]})[0]["select"]
    assert select[0]["as"] == "period"


ORDERS = "measure.jaffle.order_count"
GOVERNORS = ["metric.sales.high_value_customer_orders", "metric.sales.repeat_customer_orders"]


def test_a_raw_measure_names_the_governed_metrics_that_filter_it(runtime: Runtime) -> None:
    result = runtime.query({"select": [ORDERS]}, verbosity="minimal")
    assert result.get("assumptions", []) == [
        {"code": "GOVERNED_METRIC_EXISTS", "measure": ORDERS, "metric": metric}
        for metric in GOVERNORS
    ]
    # A metric the call selects is already in the answer.
    both = runtime.query({"select": [ORDERS, GOVERNORS[1]]})
    assert [entry["metric"] for entry in both["assumptions"]] == GOVERNORS[:1]
    assert runtime.query({"select": [GOVERNORS[0]]}).get("assumptions", []) == []


def test_a_governor_hidden_from_the_caller_withholds_the_raw_number(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = SemanticPolicyConfig(
        id="policy.hide_high_value",
        kind="object_visibility",
        object_ids=[GOVERNORS[0]],
        action="hidden",
        audiences=["external"],
    )
    monkeypatch.setattr(runtime, "_config", replace(runtime._config, semantic_policies=[policy]))
    [error] = runtime.validate({"select": [ORDERS]}, context={"audience": "external"})["errors"]
    assert error["code"] == "POLICY_DENIED"
    assert GOVERNORS[0] not in json.dumps(error) and "high value" not in json.dumps(error).lower()
    with pytest.raises(SemanticLayerError) as exc:
        runtime.query({"select": [ORDERS]}, context={"audience": "external"})
    assert exc.value.code == "POLICY_DENIED"
    # A caller who sees every governor gets the number and the names.
    seen = runtime.query({"select": [ORDERS]}, context={"audience": "internal"})
    assert [entry["metric"] for entry in seen["assumptions"]] == GOVERNORS


def test_brooklyns_share_of_revenue_matches_reference_sql(runtime: Runtime) -> None:
    share = {"kind": "share", "input": REVENUE, "over": [STORE]}
    result = runtime.query(
        {"select": [REVENUE, {"expression": share, "as": "store_share"}], "group_by": [STORE]}
    )
    reference = runtime._get_adapter().query(
        "SELECT s.store_name AS store, SUM(o.order_total_cents / 100.0) "
        "/ SUM(SUM(o.order_total_cents / 100.0)) OVER () AS share "
        "FROM jaffle_order o LEFT JOIN jaffle_store s ON o.store_id = s.store_id GROUP BY 1"
    )
    assert {row[STORE]: row["store_share"] for row in result["rows"]} == {
        row["store"]: pytest.approx(row["share"], rel=1e-12) for row in reference
    }
    assert {row[STORE]: round(row["store_share"], 4) for row in result["rows"]} == {
        "Brooklyn": 0.3478,
        "Philadelphia": 0.6522,
    }
    [stated] = [
        entry for entry in result.get("assumptions", []) if entry["code"] == "SHARE_OF_TOTAL"
    ]
    assert stated == {
        "code": "SHARE_OF_TOTAL",
        "column": "store_share",
        "input": REVENUE,
        "over": [STORE],
        "unit": "fraction",
        "rows": 2,
    }
