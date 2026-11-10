"""A scoped aggregate's predicate compares the statistic its call states.

"Revenue from customers whose average order is at least $30" writes the predicate's
``aggregation`` beside its input. The predicate takes each customer's average, never the
measure's default (their total), and a statistic the input cannot take is refused.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config, opened

REVENUE = "measure.jaffle.revenue_usd"
ORDERS = "measure.jaffle.order_count"
CUSTOMER = "entity.jaffle_customer"
VALUE = "o.order_total_cents / 100.0"


@pytest.fixture(scope="module")
def runtime() -> Iterator[Runtime]:
    rt = Runtime("jaffle_shop")
    try:
        yield opened(rt)
    finally:
        rt.close()


def _revenue_where(predicate: dict[str, Any]) -> dict[str, Any]:
    scoped = {
        "kind": "scoped_aggregate",
        "measure": REVENUE,
        "aggregation": "sum",
        "predicates": [{"entity": CUSTOMER, "over": "all_time", **predicate}],
    }
    return {"select": [{"as": "revenue", "expression": scoped}]}


def _reference(runtime: Runtime, statistic: str, value: float) -> float:
    """Revenue of the customers whose per-customer statistic is at least ``value``."""
    [row] = runtime._get_adapter().query(
        "SELECT SUM(o.order_total_cents / 100.0) AS revenue FROM jaffle_order o "
        "WHERE o.customer_id IN (SELECT o.customer_id FROM jaffle_order o GROUP BY 1 "
        f"HAVING {statistic} >= {value})"
    )
    return float(row["revenue"])


def _answer(runtime: Runtime, call: dict[str, Any]) -> float:
    [row] = runtime.query(call)["rows"]
    return float(row["revenue"])


def test_revenue_from_customers_whose_average_order_is_at_least_30(runtime: Runtime) -> None:
    call = _revenue_where({"input": REVENUE, "aggregation": "avg", "op": "gte", "value": 30})
    expected = _reference(runtime, f"AVG({VALUE})", 30)
    assert _answer(runtime, call) == pytest.approx(expected, rel=1e-9)
    # Each customer's total revenue is a different statistic and a different answer.
    assert expected == pytest.approx(74055.23, abs=0.01)
    assert _reference(runtime, f"SUM({VALUE})", 30) == pytest.approx(745646.63, abs=0.01)


# Each statistic a revenue predicate can carry, with its SQL and a threshold that splits the
# customers differently from their total revenue.
STATISTICS = [
    ("sum", {}, f"SUM({VALUE})", 1000),
    ("avg", {}, f"AVG({VALUE})", 30),
    ("min", {}, f"MIN({VALUE})", 20),
    ("max", {}, f"MAX({VALUE})", 60),
    ("median", {}, f"MEDIAN({VALUE})", 20),
    ("percentile", {"p": 0.9}, f"QUANTILE_CONT({VALUE}, 0.9)", 40),
]
# The three spellings of a predicate on a measure: its id as input, the measure shorthand,
# and the input written as a measure object.
SPELLINGS = ["input", "measure", "nested"]


def _spelled(spelling: str, measure: str, statistic: dict[str, Any]) -> dict[str, Any]:
    if spelling == "nested":
        return {"input": {"kind": "measure", "measure": measure, **statistic}}
    return {spelling: measure, **statistic}


@pytest.mark.parametrize("spelling", SPELLINGS)
@pytest.mark.parametrize(("aggregation", "parameters", "sql", "value"), STATISTICS)
def test_each_statistic_a_predicate_states_matches_reference_sql(
    runtime: Runtime,
    spelling: str,
    aggregation: str,
    parameters: dict[str, Any],
    sql: str,
    value: float,
) -> None:
    statistic = {"aggregation": aggregation, **({"parameters": parameters} if parameters else {})}
    predicate = {**_spelled(spelling, REVENUE, statistic), "op": "gte", "value": value}
    expected = _reference(runtime, sql, value)
    assert _answer(runtime, _revenue_where(predicate)) == pytest.approx(expected, rel=1e-9)


@pytest.mark.parametrize("spelling", ["input", "measure"])
def test_a_distinct_count_predicate_counts_each_customers_orders(
    runtime: Runtime, spelling: str
) -> None:
    statistic = {"aggregation": "count_distinct"}
    predicate = {**_spelled(spelling, ORDERS, statistic), "op": "gte", "value": 5}
    expected = _reference(runtime, "COUNT(DISTINCT o.order_id)", 5)
    assert _answer(runtime, _revenue_where(predicate)) == pytest.approx(expected, rel=1e-9)


def _refusal(runtime: Runtime, predicate: dict[str, Any]) -> SemanticLayerError:
    with pytest.raises(SemanticLayerError) as exc:
        runtime.query(_revenue_where(predicate))
    return exc.value


PATH = "select[0].expression.predicates[0]"


@pytest.mark.parametrize("spelling", ["input", "measure"])
def test_a_statistic_the_measure_does_not_allow_is_refused(runtime: Runtime, spelling: str) -> None:
    error = _refusal(runtime, {spelling: ORDERS, "aggregation": "count", "op": "gte", "value": 5})
    assert error.code == "UNSUPPORTED_AGGREGATION"
    assert str(error) == (
        f"{PATH}.input.aggregation: 'count' is not an aggregation of '{ORDERS}'; "
        "it allows ['count_distinct']"
    )


def test_a_statistic_beside_a_metric_input_is_refused(runtime: Runtime) -> None:
    error = _refusal(
        runtime,
        {"input": "metric.sales.aov_usd", "aggregation": "avg", "op": "gte", "value": 30},
    )
    assert error.code == "INVALID_METRIC_PREDICATE"
    assert str(error) == (
        f"{PATH}.aggregation: aggregation states a measure's statistic, and the predicate's "
        'input "metric.sales.aov_usd" is not a measure, so it would be ignored'
    )
    assert error.details["aggregation"] == "avg"
    [hint] = error.details["recovery_hints"]
    assert hint["message"].startswith("Remove aggregation: the input carries its own statistic.")


def test_a_statistic_that_contradicts_the_inputs_own_is_refused(runtime: Runtime) -> None:
    nested = {"kind": "measure", "measure": REVENUE, "aggregation": "sum"}
    error = _refusal(runtime, {"input": nested, "aggregation": "avg", "op": "gte", "value": 30})
    assert error.code == "INVALID_METRIC_PREDICATE"
    assert str(error) == (
        f'{PATH}.aggregation: the predicate\'s input {{"kind": "measure", "measure": '
        f'"{REVENUE}", "aggregation": "sum"}} states aggregation "sum", so aggregation "avg" '
        "beside it would be ignored"
    )
    [hint] = error.details["recovery_hints"]
    assert hint["message"] == "Write aggregation once, inside input."


def test_a_key_a_predicate_does_not_read_is_refused(runtime: Runtime) -> None:
    error = _refusal(runtime, {"input": REVENUE, "op": "gte", "value": 30, "bogus": 1})
    assert error.code == "INVALID_QUERY"
    assert str(error).startswith(f"{PATH}.bogus: unknown key; the keys are [")
    assert "scope_mode" not in error.details["supported_keys"]


@pytest.fixture(scope="module")
def package_metric(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    """A package metric whose predicate writes its statistic beside the measure shorthand."""
    package = copy_package_config(tmp_path_factory.mktemp("scoped"), "jaffle_shop", preseed_db=True)
    metric = {
        "as": "metric.sales.revenue_from_high_aov_customers",
        "label": "Revenue from high-AOV customers",
        "description": "Revenue from customers whose average order is at least 30 USD.",
        "kind": "aggregate",
        "temporal_role": "temporal_role.jaffle_order_time",
        "value_type": "currency",
        "expression": {
            "kind": "scoped_aggregate",
            "measure": "revenue_usd",
            "aggregation": "sum",
            "predicates": [
                {
                    "measure": "revenue_usd",
                    "aggregation": "avg",
                    "entity": CUSTOMER,
                    "scope_mode": "entity_only",
                    "op": ">=",
                    "value": 30,
                }
            ],
        },
    }
    path = Path(package) / "metrics" / "extensions" / "scoped_statistic.yml"
    path.write_text(
        yaml.safe_dump({"metrics": {"sales.revenue_from_high_aov_customers": metric}}),
        encoding="utf-8",
    )
    rt = Runtime.from_path(str(package))
    try:
        yield opened(rt)
    finally:
        rt.close()


def test_a_package_metric_predicate_compares_its_stated_statistic(
    package_metric: Runtime,
) -> None:
    [row] = package_metric.query({"select": ["metric.sales.revenue_from_high_aov_customers"]})[
        "rows"
    ]
    expected = _reference(package_metric, f"AVG({VALUE})", 30)
    assert row["revenue_from_high_aov_customers"] == pytest.approx(expected, rel=1e-9)
