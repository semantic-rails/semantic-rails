"""A metric constraint holds inside the call: a cut the compiler makes to read a value is a cut.

``allowed_group_by`` says a measure is only ever grouped by the dimensions it lists. The compiler
also groups a measure by an entity's keys inside a call: a distribution's per-entity values, an
entity filter's per-entity values (a threshold on each one decides which entities count) and a
lookup measure's per-parent values. Each such grouping is held to the measure's
``allowed_group_by`` and ``required_group_by`` like the call's own ``group_by``, and to its
``required_where`` like the call's own ``where``, and refused the same way, naming the measure,
the inner grouping and the cuts the policy allows. So is the key the answer carries beside a
name that labels an entity's rows, a conversion's operand events (read under only the filters
routed to their side) and a share's total beside an entity filter. Groupings that only
aggregate the call's own groups (a total, a share, a conversion's event matching) read nothing
finer and still answer.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import parse_semantic_expression
from semantic_rails.runtime import Runtime
from semantic_rails.schema import MetricConfig, SemanticPolicyConfig
from tests.semantic_rails import test_lookup_measures as lookups

PACKAGE = "configs/semantic_rails/jaffle_shop"
REVENUE = "measure.jaffle.revenue_usd"
ORDERS = "measure.jaffle.order_count"
SESSIONS = "measure.jaffle.session_starts"
LIFETIME_SPEND = "measure.jaffle.lifetime_spend_usd"
CUSTOMER = "entity.jaffle_customer"
STORE_ENTITY = "entity.jaffle_store"
CUSTOMER_ID = "dimension.jaffle_customer_id"
STORE = "dimension.jaffle_store_name"
STORE_ID = "dimension.jaffle_store_id"
BY_STORE = {"allowed_group_by": [STORE]}


@pytest.fixture(scope="module")
def config() -> Any:
    return replace(load_package_config(PACKAGE), semantic_policies=[])


@pytest.fixture
def engine(config: Any) -> Iterator[Any]:
    """A runtime under one metric constraint, closed at teardown."""
    built: list[Runtime] = []

    def build(
        constraint: dict[str, Any], governed: str = REVENUE, package: tuple[Any, str] | None = None
    ) -> Runtime:
        policy = SemanticPolicyConfig(
            id="policy.test.inner_grouping",
            kind="metric_constraint",
            object_ids=[governed],
            config=constraint,
        )
        base, path = package if package is not None else (config, PACKAGE)
        built.append(
            Runtime.from_config(replace(base, semantic_policies=[policy]), source_path=path)
        )
        return built[-1]

    yield build
    for runtime in built:
        runtime.close()


def _distribution(
    function: str = "median", measure: str = REVENUE, entity: str = CUSTOMER, **extra: Any
) -> dict[str, Any]:
    expression: dict[str, Any] = {
        "kind": "distribution",
        "function": function,
        "over": {"kind": "entity_value", "entity": entity, "input": measure},
    }
    if function == "percentile":
        expression["p"] = 0.9
    return {"select": [{"expression": expression, "as": "value"}], **extra}


def _entity_filter(over: str, measure: str = REVENUE, select: str = ORDERS) -> dict[str, Any]:
    entity_filter: dict[str, Any] = {
        "entity": CUSTOMER,
        "having": [{"field": measure, "op": "gte", "value": 100}],
        "over": over,
    }
    call: dict[str, Any] = {"select": [select], "where": [entity_filter]}
    if over == "period":
        call["time"] = {"grain": "month"}
    if over == "window":
        call["time"] = {"start": "2018-01-01", "end": "2018-07-01"}
    return call


def _refused(runtime: Runtime, call: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """The refusal's message and violations, the same from validate and compile."""
    result = runtime.validate(call)
    errors = result.get("errors") or []
    assert errors and errors[0]["code"] == "POLICY_DENIED", errors
    with pytest.raises(SemanticLayerError) as raised:
        runtime.compile(call)
    assert raised.value.code == "POLICY_DENIED"
    assert str(raised.value) == errors[0]["message"]
    violations = list(errors[0]["details"]["policy_violations"])
    return errors[0]["message"], violations


def _answers(runtime: Runtime, call: dict[str, Any]) -> None:
    result = runtime.validate(call)
    assert not result.get("errors"), result.get("errors")
    assert runtime.compile(call)["rendered_sql"]


# The distribution's per-entity values -------------------------------------------------------


@pytest.mark.parametrize("function", ["median", "min", "max", "percentile"])
def test_a_distribution_over_customers_is_refused_like_a_cut_by_customer(engine, function):
    runtime = engine(BY_STORE)
    message, violations = _refused(runtime, _distribution(function))
    assert message == (
        f"{REVENUE} can't be grouped by {CUSTOMER_ID}, as the distribution over {CUSTOMER} "
        f"groups it; it can be grouped only by {STORE}."
    )
    assert violations == [
        {
            "kind": "disallowed_group_by",
            "disallowed": [CUSTOMER_ID],
            "allowed": [STORE],
            "inside": f"the distribution over {CUSTOMER}",
            "object": REVENUE,
        }
    ]
    hints = runtime.validate(_distribution(function))["errors"][0]["recovery_hints"]
    assert hints[0]["message"] == f"Remove the distribution over {CUSTOMER} from the call."
    # The same customers cut directly, for comparison: the same violation kind.
    _, plain = _refused(runtime, {"select": [REVENUE], "group_by": [CUSTOMER_ID]})
    assert [row["kind"] for row in plain] == ["disallowed_group_by"]


def test_a_distribution_inside_an_allowed_group_is_still_refused(engine):
    message, _ = _refused(engine(BY_STORE), _distribution("max", group_by=[STORE]))
    assert f"can't be grouped by {CUSTOMER_ID}, as the distribution" in message


def test_a_distribution_over_an_entity_keyed_finer_than_the_allowed_cut_is_refused(engine):
    # Stores keyed by id, the policy allows their name: two stores may share a name.
    message, _ = _refused(engine(BY_STORE), _distribution(entity=STORE_ENTITY))
    assert f"can't be grouped by {STORE_ID}, as the distribution over {STORE_ENTITY}" in message


def test_a_distribution_answers_when_the_entity_key_is_an_allowed_cut(engine):
    _answers(engine({"allowed_group_by": [STORE, CUSTOMER_ID]}), _distribution())


def test_a_distribution_of_another_measure_answers(engine):
    _answers(engine(BY_STORE), _distribution(measure=ORDERS))


# Entity filters and the predicates of scoped aggregates --------------------------------------


@pytest.mark.parametrize("over", ["all_time", "period", "window"])
def test_an_entity_filter_on_the_measure_is_refused(engine, over):
    message, violations = _refused(engine(BY_STORE), _entity_filter(over))
    assert message == (
        f"{REVENUE} can't be grouped by {CUSTOMER_ID}, as the entity filter on {CUSTOMER} "
        f"groups it; it can be grouped only by {STORE}."
    )
    assert [row["inside"] for row in violations] == [f"the entity filter on {CUSTOMER}"]


def test_the_measure_cut_by_an_entity_filter_on_another_measure_answers(engine):
    # Revenue by store for customers with two orders: only order counts are read per customer.
    call = _entity_filter("all_time", measure=ORDERS, select=REVENUE)
    _answers(engine(BY_STORE), {**call, "group_by": [STORE]})


def test_a_scoped_aggregate_predicate_on_the_measure_is_refused(engine):
    predicate = {"entity": CUSTOMER, "input": REVENUE, "op": "gte", "value": 100}
    call = {
        "select": [
            {
                "expression": {
                    "kind": "scoped_aggregate",
                    "measure": ORDERS,
                    "predicates": [{**predicate, "over": "all_time"}],
                },
                "as": "orders",
            }
        ]
    }
    message, _ = _refused(engine(BY_STORE), call)
    assert f"as the entity filter on {CUSTOMER} groups it" in message


def test_an_aggregate_filter_predicate_on_the_measure_is_refused(engine):
    predicate = {
        "kind": "metric_predicate",
        "entity": CUSTOMER,
        "input": {"kind": "measure", "measure": REVENUE},
        "op": "gte",
        "value": 100,
        "over": "all_time",
    }
    call = {
        "select": [
            {
                "expression": {
                    "kind": "aggregate",
                    "measure": ORDERS,
                    "filter": {"all": [{"expression": predicate}]},
                },
                "as": "orders",
            }
        ]
    }
    message, _ = _refused(engine(BY_STORE), call)
    assert f"as the entity filter on {CUSTOMER} groups it" in message


def test_an_aggregate_over_the_measure_columns_as_a_predicate_input_is_refused(engine):
    # An inline aggregate over the order totals revenue reads, as an entity filter's input.
    total = {"kind": "column", "entity": "entity.jaffle_order", "column": "order_total_cents"}
    columns = {
        "kind": "aggregate_if",
        "aggregation": "sum",
        "condition": {
            "kind": "comparison",
            "op": ">",
            "left": total,
            "right": {"kind": "literal", "value": 0},
        },
        "value": total,
    }
    predicate = {"kind": "metric_predicate", "entity": CUSTOMER, "input": columns}
    call = {
        "select": [{"expression": {"measure": ORDERS}, "as": "orders"}],
        "metric_filters": [
            {"expression": {**predicate, "op": ">", "value": 0}, "op": "=", "value": True}
        ],
    }
    runtime = engine(BY_STORE)
    errors = runtime.validate_internal(call)["errors"]
    assert errors[0]["code"] == "POLICY_DENIED"
    assert errors[0]["message"] == (
        f"{REVENUE} can't be grouped by {CUSTOMER_ID}, as the entity filter on {CUSTOMER} "
        f"groups it; it can be grouped only by {STORE}."
    )


def test_a_joined_call_part_is_held_to_its_own_inner_groupings(engine):
    call = {
        "parts": [{"select": [ORDERS], "group_by": [STORE]}, _distribution(group_by=[STORE])],
        "join": {"on": [[STORE, STORE]]},
    }
    message, _ = _refused(engine(BY_STORE), call)
    assert f"can't be grouped by {CUSTOMER_ID}, as the distribution over {CUSTOMER}" in message


def test_a_package_metric_whose_filter_reads_the_measure_per_customer_is_refused(engine):
    # Orders of customers whose lifetime spend is at least 100: the package's own entity filter.
    call = {"select": ["metric.sales.high_value_customer_orders"]}
    message, _ = _refused(engine(BY_STORE, governed=LIFETIME_SPEND), call)
    assert message.startswith(f"{LIFETIME_SPEND} can't be grouped by {CUSTOMER_ID}, as the")


def test_a_required_group_is_kept_by_an_entity_filter_or_refused(engine):
    runtime = engine({"required_group_by": [STORE]})
    call = {**_entity_filter("all_time"), "group_by": [STORE]}
    message, violations = _refused(runtime, call)
    assert message == (
        f"{REVENUE} can only be read grouped by {STORE}, and the entity filter on {CUSTOMER} "
        "reads it across them."
    )
    assert violations[0]["kind"] == "missing_required_group_by"
    # A distribution keeps the call's groups beside the entity's keys.
    _answers(runtime, _distribution(group_by=[STORE]))


def test_a_policy_on_a_package_metric_covers_its_own_entity_filter(engine):
    call = {"select": ["metric.sales.high_value_customer_orders"]}
    message, _ = _refused(
        engine(BY_STORE, governed="metric.sales.high_value_customer_orders"), call
    )
    assert message == (
        f"metric.sales.high_value_customer_orders can't be grouped by {CUSTOMER_ID}, as the "
        f"entity filter on {CUSTOMER} groups it; it can be grouped only by {STORE}."
    )


# Groupings beside the call's own --------------------------------------------------------------


def test_the_key_a_name_brings_is_held_to_the_allowed_cuts(engine, config):
    # A store's name labels its rows, so a call by name groups by the store's key beside it.
    named = replace(
        config,
        entities=[
            replace(row, display=STORE) if row.id == STORE_ENTITY else row
            for row in config.entities
        ],
    )
    runtime = engine(BY_STORE, package=(named, PACKAGE))
    message, violations = _refused(runtime, {"select": [REVENUE], "group_by": [STORE]})
    assert message == (
        f"{REVENUE} can't be grouped by {STORE_ID}, which the answer carries beside {STORE} so "
        f"that rows of the same name stay apart; it can be grouped only by {STORE}."
    )
    assert violations[0]["beside"] == [STORE]
    _answers(
        engine({"allowed_group_by": [STORE, STORE_ID]}, package=(named, PACKAGE)),
        {
            "select": [REVENUE],
            "group_by": [STORE],
        },
    )


def test_a_conversion_reads_its_converted_events_without_the_call_filter(engine):
    brooklyn = {"field": STORE, "op": "is", "value": "Brooklyn"}
    runtime = engine({"required_where": [brooklyn]}, governed=ORDERS)
    call = {"select": ["metric.sales.session_to_order_conversion_rate_7d"], "where": [brooklyn]}
    message, violations = _refused(runtime, call)
    assert message == (
        f"{ORDERS} can only be read where {STORE} is Brooklyn, and the conversion over "
        f"{CUSTOMER} reads it without that filter."
    )
    assert violations[0]["kind"] == "missing_required_where"
    # The base events are read under it.
    _answers(engine({"required_where": [brooklyn]}, governed=SESSIONS), call)


def test_a_share_under_an_entity_filter_keeps_a_required_group(engine):
    share = {"kind": "share", "input": REVENUE, "over": [STORE]}
    call = {
        **_entity_filter("all_time", measure=ORDERS, select=REVENUE),
        "select": [REVENUE, {"expression": share, "as": "share"}],
        "group_by": [STORE],
    }
    message, violations = _refused(engine({"required_group_by": [STORE]}), call)
    assert message == (
        f"{REVENUE} can only be read grouped by {STORE}, and the share in 'share' reads it "
        "across them."
    )
    assert violations[0]["share"] == "share"


def _with_metric(config: Any, metric_id: str, expression: dict[str, Any]) -> tuple[Any, str]:
    """The package with one more package metric (in the package's own form)."""
    parsed = parse_semantic_expression(expression, context="query")
    metric = MetricConfig(id=metric_id, kind="derived", expression=parsed)
    return replace(config, metric_recipes=[*config.metric_recipes, metric]), PACKAGE


def test_a_policy_on_a_package_metric_covers_its_own_share(engine, config):
    metric = "metric.test.store_revenue_share"
    share = {"kind": "share", "input": {"kind": "measure", "measure": REVENUE}, "over": [STORE]}
    package = _with_metric(config, metric, share)
    runtime = engine({"required_group_by": [STORE]}, governed=metric, package=package)
    message, _ = _refused(runtime, {"select": [metric], "group_by": [STORE]})
    assert message == (
        f"{metric} can only be read grouped by {STORE}, and the share in 'store_revenue_share' reads it "
        "across them."
    )


def test_a_policy_on_a_conversion_operand_reads_only_its_own_side(engine, config):
    brooklyn = {"field": STORE, "op": "is", "value": "Brooklyn"}
    sessions = "metric.test.sessions"
    package = _with_metric(config, sessions, {"kind": "measure", "measure": SESSIONS})
    conversion = {
        "kind": "conversion",
        "entity": CUSTOMER,
        "base": sessions,
        "converted": ORDERS,
        "window": {"unit": "day", "value": 7},
        "matching_mode": "first_converted_after_base",
    }
    call = {"select": [{"expression": conversion, "as": "rate"}], "where": [brooklyn]}
    # The base sessions are read where the store is Brooklyn; the converted orders are not.
    _answers(engine({"required_where": [brooklyn]}, governed=sessions, package=package), call)
    message, _ = _refused(
        engine({"required_where": [brooklyn]}, governed=ORDERS, package=package), call
    )
    assert message.endswith(f"the conversion over {CUSTOMER} reads it without that filter.")


# A lookup measure's per-parent values ---------------------------------------------------------


@pytest.fixture(scope="module")
def lookup_package(tmp_path_factory) -> Any:
    root = lookups._seed(lookups._write(tmp_path_factory.mktemp("inner_lookup") / lookups.NS))
    return load_package_config(str(root)), str(root)


PREMIUM = lookups._measure("premium_amount")
CLAIM_COUNT = lookups._measure("claim_count")
CLAIM_KEY = lookups.CLAIM_KEY
LINE_OF_BUSINESS = f"dimension.{lookups.NS}_coverage_line_of_business"
PER_CLAIM = {"select": [lookups.CARRIED], "group_by": [CLAIM_KEY]}


def test_a_lookup_of_the_measure_per_parent_is_refused(engine, lookup_package):
    # Premiums may be read per claim, never per coverage: each claim carries its coverage's total.
    runtime = engine({"allowed_group_by": [CLAIM_KEY]}, governed=PREMIUM, package=lookup_package)
    message, violations = _refused(runtime, PER_CLAIM)
    assert message == (
        f"{PREMIUM} can't be grouped by {lookups.COVERAGE_KEY}, as the lookup measure "
        f"{lookups.CARRIED} groups it; it can be grouped only by {CLAIM_KEY}."
    )
    assert violations[0]["inside"] == f"the lookup measure {lookups.CARRIED}"
    _answers(runtime, {"select": [CLAIM_COUNT], "group_by": [CLAIM_KEY]})


def test_a_lookup_that_reads_the_measure_without_its_required_filter_is_refused(
    engine, lookup_package
):
    required = {"field": LINE_OF_BUSINESS, "op": "is", "value": "auto"}
    runtime = engine({"required_where": [required]}, governed=PREMIUM, package=lookup_package)
    message, violations = _refused(runtime, {**PER_CLAIM, "where": [required]})
    assert message.endswith(
        f"and the lookup measure {lookups.CARRIED} reads it without that filter."
    )
    assert violations[0]["kind"] == "missing_required_where"
    # The distribution's per-entity values keep the call's where.
    call = _distribution(measure=PREMIUM, entity=lookups.COVERAGE, where=[required])
    _answers(runtime, call)


# What reads nothing finer than the call ------------------------------------------------------


def test_a_total_beside_allowed_groups_answers(engine):
    total = {"kind": "total", "input": REVENUE, "over": [STORE]}
    call = {"select": [REVENUE, {"expression": total, "as": "all_stores"}], "group_by": [STORE]}
    _answers(engine(BY_STORE), call)


def test_a_conversion_matches_events_per_customer_without_reading_a_value(engine):
    call = {"select": ["metric.sales.session_to_order_conversion_rate_7d"]}
    _answers(engine(BY_STORE, governed=SESSIONS), call)


# A caller no such policy restricts ----------------------------------------------------------


def _package_examples() -> list[tuple[str, dict[str, Any]]]:
    out = []
    for path in sorted(Path(PACKAGE, "examples").glob("*.yml")):
        for name, row in (yaml.safe_load(path.read_text()) or {}).get("examples", {}).items():
            out.append((f"{path.stem}.{name}", row["query"]))
    for path in sorted(Path("examples").glob("*.json")):
        out.append((path.stem, json.loads(path.read_text())))
    return out


EXAMPLES = _package_examples()
NOW = "2026-06-01T00:00:00Z"


def test_the_package_has_examples_to_check():
    assert len(EXAMPLES) > 5


@pytest.fixture(scope="module")
def unrestricted() -> Iterator[tuple[Runtime, Runtime]]:
    """The package with its shipped policies, read by a caller none restricts, and the
    package with no policies at all."""
    shipped = load_package_config(PACKAGE)
    pair = (
        Runtime.from_config(shipped, source_path=PACKAGE),
        Runtime.from_config(replace(shipped, semantic_policies=[]), source_path=PACKAGE),
    )
    yield pair
    for runtime in pair:
        runtime.close()


@pytest.mark.parametrize(("name", "call"), EXAMPLES, ids=[name for name, _ in EXAMPLES])
def test_a_caller_no_constraint_restricts_gets_the_same_sql(unrestricted, name, call):
    governed, open_ = unrestricted
    try:
        expected = open_.compile(call, now=NOW)["rendered_sql"]
    except SemanticLayerError as refused:
        with pytest.raises(SemanticLayerError) as raised:
            governed.compile(call, now=NOW)
        assert (raised.value.code, str(raised.value)) == (refused.code, str(refused))
        return
    assert governed.compile(call, now=NOW)["rendered_sql"] == expected
