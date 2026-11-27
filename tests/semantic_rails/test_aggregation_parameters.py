"""An aggregation's ``parameters``: a key it doesn't read is refused, never dropped.

Dropped, it answered another question: "average title length" written as an ``avg`` with
``{"expression": "LENGTH(title)"}`` read the plain average of the measure.
"""

from __future__ import annotations

import pytest
import yaml

from semantic_rails.config_validation import resolve_package_reference, validate_config_report
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config, opened
from tests.semantic_rails.test_runtime_and_metadata import jaffle_snapshot as jaffle_snapshot

REVENUE = "measure.jaffle.revenue_usd"  # sum; allows avg, min, max, median, percentile
ORDERS = "measure.jaffle.order_count"  # count_distinct only
STOCK = "measure.jaffle.inventory_on_hand_eop"  # last_value; allows first_value
CUSTOMER = "entity.jaffle_customer"
LENGTH = {"expression": "LENGTH(store_name)"}
# Each argument shape a parameter can take, beside the key no aggregation reads but percentile.
SHAPES = {
    "column": {"column": "store_name"},
    "expression": LENGTH,
    "expression_ast": {
        "expression": {
            "kind": "call",
            "name": "LENGTH",
            "args": [{"kind": "column", "column": "store_name"}],
        }
    },
    "literal": {"value": 3},
    "p": {"p": 0.9},
    "two": {"p": 0.9, **LENGTH},
}
STATISTICS = [
    *((REVENUE, aggregation) for aggregation in ("sum", "avg", "min", "max", "median")),
    (ORDERS, "count_distinct"),
    (STOCK, "last_value"),
    (STOCK, "first_value"),
]


@pytest.fixture(scope="module")
def mcp(jaffle_snapshot):
    adapter = SemanticLayerMCPAdapter(
        opened(Runtime.from_snapshot(jaffle_snapshot, package_id="jaffle_shop"))
    )
    yield adapter
    adapter.close()


def _one(expression):
    return {"select": [{"expression": expression, "as": "x"}]}


def _measure(measure, aggregation, parameters=None, kind="measure"):
    expression = {"kind": kind, "measure": measure, "aggregation": aggregation}
    if parameters is not None:
        expression["parameters"] = parameters
    return expression


def _refusal(mcp, query):
    """The one error a validate and a run both answer, with its hint."""
    responses = [
        mcp.call_tool("execute", {"query": query, "mode": mode}) for mode in ("validate", "run")
    ]
    assert [response["ok"] for response in responses] == [False, False]
    [error] = responses[0]["errors"]
    assert [e["code"] for e in responses[1]["errors"]] == [error["code"]]
    return error


def test_average_of_an_expression_parameter_is_refused_naming_it(mcp):
    # The case as an agent wrote it: the average a LENGTH, on a measure that allows avg.
    error = _refusal(
        mcp,
        {
            "select": [
                {
                    "expression": _measure(REVENUE, "avg", LENGTH, "aggregate"),
                    "as": "avg_title_length",
                }
            ]
        },
    )
    assert error["code"] == "UNSUPPORTED_AGGREGATION"
    assert error["message"] == (
        "avg takes no parameters: parameters.expression on measure.jaffle.revenue_usd would be "
        "ignored."
    )
    assert error["details"] == {
        "aggregation": "avg",
        "measure": REVENUE,
        "unread_parameters": ["parameters.expression"],
        "parameters_accepted": [],
    }
    [hint] = error["recovery_hints"]
    assert hint["kind"] == "remove_unread_parameters"
    assert hint["message"] == (
        "If the avg of measure.jaffle.revenue_usd itself is meant, remove parameters.expression; "
        "another value is another measure (find it with discover), or an aggregate_if: "
        '{"kind": "aggregate_if", "aggregation": "avg", "value": <expression>}.'
    )


@pytest.mark.parametrize("kind", ["measure", "aggregate", "scoped_aggregate"])
@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize(("measure", "aggregation"), STATISTICS)
def test_every_statistic_but_percentile_refuses_every_parameter(
    mcp, measure, aggregation, shape, kind
):
    error = _refusal(mcp, _one(_measure(measure, aggregation, SHAPES[shape], kind)))
    unread = sorted(f"parameters.{key}" for key in SHAPES[shape])
    assert (error["code"], error["details"]["unread_parameters"]) == (
        "UNSUPPORTED_AGGREGATION",
        unread,
    )
    assert error["message"].startswith(
        f"{aggregation} takes no parameters: {', '.join(unread)} on {measure}"
    )


@pytest.mark.parametrize("shape", ["column", "expression", "literal", "two"])
def test_percentile_refuses_a_key_beside_p(mcp, shape):
    error = _refusal(mcp, _one(_measure(REVENUE, "percentile", SHAPES[shape])))
    assert error["details"]["unread_parameters"] == sorted(
        f"parameters.{key}" for key in SHAPES[shape] if key != "p"
    )
    assert error["message"].startswith("percentile takes only parameters.p: ")
    assert error["details"]["parameters_accepted"] == ["p"]
    [hint] = error["recovery_hints"]
    assert "aggregate_if" not in hint["message"]  # an aggregate_if takes no p


def test_the_default_aggregation_reads_parameters_as_an_override_does(mcp):
    # No ``aggregation``: the measure's own (sum) still takes no parameters.
    error = _refusal(mcp, _one({"kind": "measure", "measure": REVENUE, "parameters": LENGTH}))
    assert error["details"]["aggregation"] == "sum"


def test_a_metric_that_names_a_parameter_its_statistic_doesnt_read_fails_validation(tmp_path):
    package_dir = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    path = package_dir / "metrics" / "extensions" / "derived_metrics.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["metrics"]["sales.average_store_name_length"] = {
        "as": "metric.sales.average_store_name_length",
        "label": "Average store name length",
        "description": "The average length of the store name over orders.",
        "kind": "derived",
        "value_type": "number",
        "temporal_role": "temporal_role.jaffle_order_time",
        "expression": _measure(REVENUE, "avg", LENGTH, "aggregate"),
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    report = validate_config_report(resolve_package_reference(path=str(package_dir)))

    [failed] = [probe for probe in report["probes"] if not probe["ok"]]
    assert failed["object_id"] == "metric.sales.average_store_name_length"
    assert failed["error"]["code"] == "UNSUPPORTED_AGGREGATION"
    assert (
        "parameters.expression on measure.jaffle.revenue_usd would be ignored"
        in (failed["error"]["message"])
    )


def _distribution(function, **keys):
    over = {"kind": "entity_value", "entity": CUSTOMER, "input": REVENUE}
    return {"kind": "distribution", "function": function, "over": over, **keys}


@pytest.mark.parametrize(
    ("function", "keys", "unread"),
    [
        ("sum", {"parameters": LENGTH}, ["parameters.expression"]),
        ("percentile", {"p": 0.9, "parameters": LENGTH}, ["parameters.expression"]),
    ],
)
def test_a_distribution_refuses_a_parameter_its_function_doesnt_read(mcp, function, keys, unread):
    error = _refusal(mcp, _one(_distribution(function, **keys)))
    assert (error["code"], error["details"]["unread_parameters"]) == (
        "UNSUPPORTED_AGGREGATION",
        unread,
    )
    assert error["message"].endswith(f"{', '.join(unread)} on the distribution would be ignored.")
    assert "percentile_p" not in error["details"]
    [hint] = error["recovery_hints"]
    assert hint["message"] == (
        f"If the {function} of the distribution's values itself is meant, remove "
        f"{', '.join(unread)}; another value is another measure (find it with discover)."
    )


@pytest.mark.parametrize(("aggregation", "unread_answer"), [("avg", 12.504074), ("median", 6.36)])
def test_a_p_on_a_measure_that_allows_percentile_is_offered_the_percentile(
    mcp, aggregation, unread_answer
):
    # "What is our 90th-percentile order value?" written as an avg (or median) with a p:
    # removing the p alone would answer the plain statistic, not the p90 (26.50).
    error = _refusal(mcp, _one(_measure(REVENUE, aggregation, {"p": 0.9})))
    assert error["code"] == "UNSUPPORTED_AGGREGATION"
    assert error["message"] == (
        f"{aggregation} takes no parameters: parameters.p on measure.jaffle.revenue_usd would be "
        "ignored."
    )
    assert error["details"]["percentile_p"] == 0.9
    [hint] = error["recovery_hints"]
    assert hint["kind"] == "remove_unread_parameters"
    assert hint["message"] == (
        'For the p-th percentile, write "aggregation": "percentile" with "parameters": '
        f'{{"p": 0.9}}; to keep {aggregation}, remove parameters.p.'
    )
    # Either way out answers: the percentile asked for, or the stated statistic.
    assert _value(mcp, _measure(REVENUE, "percentile", {"p": 0.9})) == pytest.approx(26.5)
    assert _value(mcp, _measure(REVENUE, aggregation)) == pytest.approx(unread_answer, abs=1e-6)


@pytest.mark.parametrize(
    ("function", "keys", "p_key"),
    [("avg", {"p": 0.9}, "p"), ("median", {"parameters": {"p": 0.9}}, "parameters.p")],
)
def test_a_p_on_a_distribution_is_offered_the_percentile(mcp, function, keys, p_key):
    # "p90 of customer revenue" written as a distribution avg with a p.
    error = _refusal(mcp, _one(_distribution(function, **keys)))
    assert (error["code"], error["details"]["unread_parameters"]) == (
        "UNSUPPORTED_AGGREGATION",
        [p_key],
    )
    assert error["message"] == (
        f"{function} takes no parameters: {p_key} on the distribution would be ignored."
    )
    [hint] = error["recovery_hints"]
    assert hint["message"] == (
        'For the p-th percentile, write "function": "percentile" with "p": 0.9; '
        f"to keep {function}, remove {p_key}."
    )
    assert _value(mcp, _distribution("percentile", p=0.9)) == pytest.approx(1548.828)


def test_a_percentile_with_an_expression_says_removing_it_reads_the_measure_itself(mcp):
    # "P90 order value before tax" as a percentile of revenue with an expression: removing it
    # reads the p90 of revenue; the pre-tax value is another measure.
    error = _refusal(
        mcp,
        _one(_measure(REVENUE, "percentile", {"p": 0.9, "expression": "subtotal_cents / 100.0"})),
    )
    assert error["code"] == "UNSUPPORTED_AGGREGATION"
    assert error["message"] == (
        "percentile takes only parameters.p: parameters.expression on measure.jaffle.revenue_usd "
        "would be ignored."
    )
    assert "percentile_p" not in error["details"]
    [hint] = error["recovery_hints"]
    assert hint["message"] == (
        "If the percentile of measure.jaffle.revenue_usd itself is meant, remove "
        "parameters.expression; another value is another measure (find it with discover)."
    )


def test_a_p_on_a_measure_without_percentile_gets_no_percentile_advice(mcp):
    # order_count allows only count_distinct: no percentile to offer.
    error = _refusal(mcp, _one(_measure(ORDERS, "count_distinct", {"p": 0.9})))
    assert error["code"] == "UNSUPPORTED_AGGREGATION"
    assert error["message"] == (
        "count_distinct takes no parameters: parameters.p on measure.jaffle.order_count would be "
        "ignored."
    )
    assert "percentile_p" not in error["details"]
    [hint] = error["recovery_hints"]
    assert "percentile" not in hint["message"]
    assert hint["message"] == (
        "If the count_distinct of measure.jaffle.order_count itself is meant, remove "
        "parameters.p; another value is another measure (find it with discover), or an "
        'aggregate_if: {"kind": "aggregate_if", "aggregation": "count_distinct", '
        '"value": <expression>}.'
    )


def test_a_percentile_threshold_refuses_a_key_it_doesnt_read(mcp):
    predicate = {
        "kind": "metric_predicate",
        "entity": CUSTOMER,
        "over": "all_time",
        "input": _measure("measure.jaffle.lifetime_spend_usd", "sum"),
        "op": ">=",
        "value": {"kind": "percentile", "p": 0.9, "aggregation": "avg"},
    }
    error = _refusal(
        mcp,
        _one(
            {
                "kind": "scoped_aggregate",
                "measure": REVENUE,
                "aggregation": "sum",
                "predicates": [predicate],
            }
        ),
    )
    assert error["code"] == "INVALID_EXPRESSION_AST"
    assert error["message"] == (
        "metric_predicate inline percentile threshold takes only 'p' and 'measure': "
        "['aggregation'] would be ignored"
    )
    assert [hint["message"] for hint in error["recovery_hints"]] == [
        "Remove ['aggregation'] from the threshold."
    ]


def _value(mcp, expression):
    response = mcp.call_tool("execute", {"query": _one(expression), "mode": "run"})
    assert response["ok"], response.get("errors")
    [row] = response["rows"]
    return row["x"]


def test_what_each_statistic_reads_still_answers(mcp):
    # The same numbers as hand SQL on the seed (order_total_cents / 100.0 per order).
    assert _value(mcp, _measure(REVENUE, "avg", {})) == _value(mcp, _measure(REVENUE, "avg"))
    assert _value(mcp, _measure(REVENUE, "avg")) == pytest.approx(12.504074, abs=1e-6)
    assert _value(mcp, _measure(REVENUE, "percentile", {"p": 0.9})) == pytest.approx(26.5)
    assert _value(mcp, _distribution("percentile", p=0.9)) == pytest.approx(1548.828)
    assert _value(mcp, _distribution("percentile", parameters={"p": 0.9})) == pytest.approx(
        1548.828
    )
