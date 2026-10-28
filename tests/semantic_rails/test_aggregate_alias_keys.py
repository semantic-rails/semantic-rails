"""Aggregates of one measure that differ by filter or clock keep their own columns.

An aggregate's leaf column was keyed on its measure, aggregation and parameters
only. Two aggregates of one measure that differed only by ``filter``, or only by
``temporal_role``, shared one column, and the first one selected answered for
both: a "share of orders" ratio returned 1.0. Conversions are keyed by their
operands, so two conversions that differed only by an operand filter collided
the same way.
"""

from __future__ import annotations

import dataclasses

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config, opened

_ORDER_TIME = "temporal_role.jaffle_order_time"
_FIRST_ORDER = "temporal_role.jaffle_customer_first_order_at"
_SESSION_TIME = "temporal_role.jaffle_session_started_at"


def _new_customer(value: bool) -> dict:
    return {"field": "dimension.jaffle_order_is_new_customer_order", "op": "is", "value": value}


_ORDERS = {"kind": "aggregate", "measure": "measure.jaffle.order_count"}
_NEW_ORDERS = {**_ORDERS, "filter": {"all": [_new_customer(True)]}}


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    path = copy_package_config(tmp_path_factory.mktemp("package"), "jaffle_shop", preseed_db=True)
    runtime = Runtime.from_path(str(path))
    yield opened(runtime)
    runtime.close()


def _by_year(rows: list[dict], select: list[dict]) -> dict[str, list]:
    rows = sorted(rows, key=lambda row: str(row["period"]))
    return {item["as"]: [row[item["as"]] for row in rows] for item in select}


def _query(runtime, select: list[dict], role: str = _ORDER_TIME, config=None) -> dict[str, list]:
    query = {"select": select, "time": {"temporal_role": role, "grain": "year"}}
    if config is None:
        rows = runtime.query(query)["rows"]
    else:
        compiled = compile_query(config, Registry(config), runtime.engine_payload(query))
        rows = runtime._get_adapter().query(compiled["sql"])
    return _by_year(rows, select)


def _each_alone(runtime, select: list[dict], **kwargs) -> dict[str, list]:
    return {item["as"]: _query(runtime, [item], **kwargs)[item["as"]] for item in select}


def test_a_share_of_one_measure_under_a_filter_is_not_one(runtime):
    # This returned [1.0, 1.0].
    share = {"kind": "ratio", "numerator": _NEW_ORDERS, "denominator": _ORDERS}
    ratio = _query(runtime, [{"expression": share, "as": "share"}])["share"]
    counts = _each_alone(
        runtime,
        [{"expression": _NEW_ORDERS, "as": "new"}, {"expression": _ORDERS, "as": "all"}],
    )

    assert ratio == pytest.approx(
        [n / d for n, d in zip(counts["new"], counts["all"], strict=True)]
    )
    assert all(value < 1 for value in ratio)


def test_a_filtered_metric_beside_its_unfiltered_measure_keeps_its_filter(runtime):
    # repeat_customer_orders is order_count (count_distinct) under a filter.
    select = [
        {"expression": "metric.sales.repeat_customer_orders", "as": "repeat"},
        {"expression": {**_ORDERS, "aggregation": "count_distinct"}, "as": "orders"},
    ]
    together = _query(runtime, select)
    alone = _each_alone(runtime, select)

    assert together == alone
    assert alone["repeat"] != alone["orders"]


def _two_clock_config(runtime):
    # order_count timed by its order time or by its customer's first order.
    return dataclasses.replace(
        runtime.config,
        measures=[
            dataclasses.replace(row, compatible_temporal_roles=[_ORDER_TIME, _FIRST_ORDER])
            if row.id == "measure.jaffle.order_count"
            else row
            for row in runtime.config.measures
        ],
    )


def test_aggregates_of_one_measure_that_differ_by_clock_keep_their_own_columns(runtime):
    # The query's clock is the sessions', which order_count lacks, so each order
    # aggregate falls back to its own clock; session_starts keeps the query valid.
    select = [
        {"expression": {**_ORDERS, "temporal_role": _ORDER_TIME}, "as": "by_order"},
        {"expression": {**_ORDERS, "temporal_role": _FIRST_ORDER}, "as": "by_first_order"},
        {
            "expression": {"kind": "aggregate", "measure": "measure.jaffle.session_starts"},
            "as": "sessions",
        },
    ]
    config = _two_clock_config(runtime)
    together = [
        _query(runtime, ordered, role=_SESSION_TIME, config=config)
        for ordered in (select, [select[1], select[0], select[2]])
    ]
    alone = {
        item["as"]: _query(runtime, [item, select[2]], role=_SESSION_TIME, config=config)[
            item["as"]
        ]
        for item in select[:2]
    }

    for result in together:
        assert {key: result[key] for key in alone} == alone
    assert alone["by_order"] != alone["by_first_order"]


def test_an_explicit_clock_equal_to_the_querys_and_no_clock_both_answer(runtime):
    select = [
        {"expression": {**_ORDERS, "temporal_role": _ORDER_TIME}, "as": "explicit"},
        {"expression": _ORDERS, "as": "implicit"},
    ]
    together = _query(runtime, select)

    assert together["explicit"] == together["implicit"]
    assert together["implicit"]


def _order_conversion(converted_filter: list[dict]) -> dict:
    converted = dict(_ORDERS)
    if converted_filter:
        converted["filter"] = {"all": converted_filter}
    return {
        "kind": "conversion",
        "entity": "entity.jaffle_customer",
        "window": {"unit": "day", "value": 28},
        "matching_mode": "first_converted_after_base",
        "base": _NEW_ORDERS,
        "converted": converted,
    }


def test_conversions_that_differ_by_an_operand_filter_keep_their_own_columns(runtime):
    select = [
        {"expression": _order_conversion([_new_customer(False)]), "as": "to_repeat"},
        {"expression": _order_conversion([]), "as": "to_any"},
    ]
    together = [_query(runtime, ordered) for ordered in (select, select[::-1])]
    alone = _each_alone(runtime, select)

    for result in together:
        assert result == alone
    assert alone["to_repeat"] != alone["to_any"]


_STORE = "dimension.jaffle_store_name"


@pytest.mark.parametrize("name", ["t", "T", "g1", "m1", "m2", "p1"])
@pytest.mark.parametrize("limited", [False, True], ids=["all", "top-1"])
def test_a_column_named_like_an_internal_name_reads_its_own_values(runtime, name, limited):
    """The statement names the period, group keys, measures and result filters ``t``, ``g1``,
    ``m1`` and ``p1``. A call's column of one of those names used to read the period or a key
    (store names for an order count), or be refused beside a having and a limit; it reads its
    own values, as under any other name."""

    def call(alias: str) -> dict:
        query = {
            "select": [
                {"expression": "measure.jaffle.order_count", "as": alias},
                "measure.jaffle.revenue_usd",
            ],
            "group_by": [_STORE],
            "time": {"grain": "month", "start": "2017-01-01", "end": "2017-04-01"},
            "having": [{"field": alias, "op": "gte", "value": 2}],
        }
        if limited:
            query.update(order_by=[{"field": alias, "direction": "desc"}], limit=1)
        return query

    def rows(alias: str) -> list:
        answer = runtime.query(call(alias), now="2017-09-04T12:00:00Z")
        return [
            (row[_STORE], row["period"], row[alias], row["revenue_usd"]) for row in answer["rows"]
        ]

    assert rows(name) == rows("orders")
