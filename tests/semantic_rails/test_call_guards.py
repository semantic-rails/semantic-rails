"""Guards on what a call asks for: column names, statistic overrides, governed measures,
shares of a total, rank by key."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

from semantic_rails.errors import SemanticLayerError
from semantic_rails.issues import ASSUMPTION, CODES
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
    assert result["assumptions"] == [
        {
            "code": "GOVERNED_METRIC_EXISTS",
            "measure": ORDERS,
            "metrics": GOVERNORS,
            "count": 2,
            "message": f"{ORDERS} is also defined as a governed metric ({', '.join(GOVERNORS)}). "
            "Select one to use the package's definition.",
        }
    ]
    # A metric the call selects is already in the answer.
    both = runtime.query({"select": [ORDERS, GOVERNORS[1]]})
    assert [entry["metrics"] for entry in both.get("assumptions", [])] == [GOVERNORS[:1]]
    assert "Select it " in both["assumptions"][0]["message"]
    assert runtime.query({"select": [GOVERNORS[0]]}).get("assumptions", []) == []


def test_many_governed_metrics_are_one_entry_naming_three(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipes = {row.id: row for row in runtime._config.metric_recipes}
    copies = [replace(recipes[GOVERNORS[0]], id=f"{GOVERNORS[0]}_{n}") for n in range(10)]
    config = replace(runtime._config, metric_recipes=[*recipes.values(), *copies])
    monkeypatch.setattr(runtime, "_config", config)
    [entry] = runtime.validate({"select": [ORDERS]})["assumptions"]
    assert (entry["metrics"], entry["count"]) == ([*GOVERNORS, f"{GOVERNORS[0]}_0"], 12)
    assert entry["message"].startswith(f"{ORDERS} is also defined as a governed metric (")
    assert "and 9 more). Select one" in entry["message"]


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
    assert [entry["metrics"] for entry in seen["assumptions"]] == [GOVERNORS]


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
    [stated] = [entry for entry in result["assumptions"] if entry["code"] == "SHARE_OF_TOTAL"]
    assert stated == {
        "code": "SHARE_OF_TOTAL",
        "column": "store_share",
        "input": REVENUE,
        "over": [STORE],
        "unit": "fraction",
        "rows": 2,
        "message": f"store_share is a fraction of the total of {REVENUE} across {STORE} in the "
        "rows the call keeps (2 rows per total).",
    }


@pytest.mark.parametrize(
    "call",
    [
        # A result threshold reads the raw count per store.
        {
            "group_by": [STORE],
            "having": [{"field": ORDERS, "op": "gte", "value": 30000}],
        },
        # An entity threshold reads it per customer.
        {
            "select": [REVENUE],
            "where": [
                {
                    "entity": "entity.jaffle_customer",
                    "having": [{"field": ORDERS, "op": "gte", "value": 5}],
                    "over": "all_time",
                }
            ],
        },
        # A scoped aggregate's predicate reads it inside a select.
        {
            "select": [
                {
                    "expression": {
                        "kind": "scoped_aggregate",
                        "measure": REVENUE,
                        "predicates": [
                            {
                                "entity": "entity.jaffle_customer",
                                "input": ORDERS,
                                "op": "gte",
                                "value": 5,
                                "over": "all_time",
                            }
                        ],
                    },
                    "as": "revenue_of_regulars",
                }
            ]
        },
        {
            "select": [
                {
                    "expression": {"kind": "ratio", "numerator": REVENUE, "denominator": ORDERS},
                    "as": "aov",
                }
            ]
        },
        {
            "select": [
                {
                    "expression": {
                        "kind": "distribution",
                        "function": "avg",
                        "over": {
                            "kind": "entity_value",
                            "entity": "entity.jaffle_customer",
                            "input": ORDERS,
                        },
                    },
                    "as": "orders_per_customer",
                }
            ]
        },
        {
            "select": [
                {
                    "expression": {
                        "kind": "case",
                        "whens": [
                            {
                                "when": {
                                    "kind": "comparison",
                                    "op": ">",
                                    "left": ORDERS,
                                    "right": {"kind": "literal", "value": 0},
                                },
                                "then": {"kind": "literal", "value": 1},
                            }
                        ],
                        "else": {"kind": "literal", "value": 0},
                    },
                    "as": "any_orders",
                }
            ]
        },
    ],
    ids=["having", "entity-filter", "scoped-predicate", "ratio", "distribution", "case"],
)
def test_no_threshold_reads_a_raw_measure_a_hidden_metric_governs(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, call: dict[str, Any]
) -> None:
    policy = SemanticPolicyConfig(
        id="policy.hide_high_value",
        kind="object_visibility",
        object_ids=[GOVERNORS[0]],
        action="hidden",
        audiences=["external"],
    )
    monkeypatch.setattr(runtime, "_config", replace(runtime._config, semantic_policies=[policy]))
    [error] = runtime.validate(call, context={"audience": "external"})["errors"]
    assert error["code"] == "POLICY_DENIED"
    assert GOVERNORS[0] not in json.dumps(error) and "high value" not in json.dumps(error).lower()
    # A package metric reads the measure on its own terms: it is the caller's to use.
    governed = {**call, "select": [GOVERNORS[1]]} if "select" not in call else None
    if governed is not None:
        assert runtime.validate(
            {"select": [GOVERNORS[1]], "group_by": [STORE]}, context={"audience": "external"}
        )["ok"]


def test_a_literal_that_looks_like_a_reference_is_data(
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
    constant = {"kind": "literal", "value": {"measure": ORDERS}}
    call = {"select": [REVENUE, {"expression": constant, "as": "constant"}]}
    assert runtime.validate(call, context={"audience": "external"})["ok"]


NOW = "2017-06-14T12:00:00Z"
CUSTOMER = "entity.jaffle_customer"
# A call whose answer states each assumption code; a code added to the registry needs one here.
STATED = {
    "AGGREGATION_OVERRIDE": {
        "select": [{"expression": {"kind": "measure", "measure": REVENUE, "aggregation": "max"}}]
    },
    "ENTITY_FILTER_OVER": {
        "select": [REVENUE],
        "where": [
            {
                "entity": CUSTOMER,
                "having": [{"field": ORDERS, "op": "gte", "value": 2}],
                "over": "all_time",
            }
        ],
    },
    "GOVERNED_METRIC_EXISTS": {"select": [ORDERS]},
    "GROUPED_BY_KEY": {"select": [REVENUE], "group_by": ["dimension.jaffle_customer_name"]},
    "OBSERVED_PERIODS_ONLY": {
        "select": [REVENUE],
        "group_by": ["dimension.jaffle_order_ordered_at"],
        "time": {"grain": "day", "last": {"unit": "day", "value": 2}},
    },
    "PARTIAL_PERIOD": {"select": [REVENUE], "time": {"this": "month"}},
    "SHARE_OF_TOTAL": {
        "select": [{"expression": {"kind": "share", "input": REVENUE, "over": [STORE]}}],
        "group_by": [STORE],
    },
    "SHOWN_BY_KEY": {"select": [REVENUE], "group_by": ["dimension.jaffle_store_id"]},
    "WINDOW_TOTAL": {"select": [REVENUE], "time": {"last": {"unit": "month", "value": 1}}},
}


@pytest.mark.parametrize("code", sorted(c for c, kind in CODES.items() if kind == ASSUMPTION))
def test_every_assumption_an_answer_states_says_it_in_words(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    # The customer entity names its rows by customer_name, as a package declares with display.
    entities = [
        replace(row, display="dimension.jaffle_customer_name") if row.id == CUSTOMER else row
        for row in runtime._config.entities
    ]
    monkeypatch.setattr(runtime, "_config", replace(runtime._config, entities=entities))
    stated = [row for row in runtime.query(STATED[code], now=NOW)["assumptions"]]
    assert code in [row["code"] for row in stated]
    assert all(isinstance(row["message"], str) and row["message"].strip() for row in stated)


def test_a_key_beside_another_of_its_entity_s_dimensions_is_not_shown_by_key(
    runtime: Runtime,
) -> None:
    call = {"select": [REVENUE], "group_by": ["dimension.jaffle_store_id", STORE]}
    assert runtime.validate(call).get("assumptions", []) == []
    [shown] = runtime.validate({**call, "group_by": call["group_by"][:1]})["assumptions"]
    assert shown == {
        "code": "SHOWN_BY_KEY",
        "entity": "entity.jaffle_store",
        "dimension": "dimension.jaffle_store_id",
        "message": "entity.jaffle_store declares no name, so its rows show only "
        "dimension.jaffle_store_id.",
    }
