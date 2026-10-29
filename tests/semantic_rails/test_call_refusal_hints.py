"""Each refusal names its path and one hint the caller can follow, in the call's own terms."""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import semantic_rails
from semantic_rails.ast import normalize_partial_query
from semantic_rails.compiler_parts import binding
from semantic_rails.diagnostics import enrich_column_expression
from semantic_rails.embedding import RequestContext
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.package_snapshot import load_package_snapshot
from semantic_rails.query_call import V2_POINTER, call_kinds
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig
from tests.semantic_rails.conftest import copy_package_config, opened

NOW = "2017-09-04T12:00:00Z"
ORDERS = "measure.jaffle.order_count"
REVENUE = "measure.jaffle.revenue_usd"


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    path = copy_package_config(tmp_path_factory.mktemp("package"), "jaffle_shop", preseed_db=True)
    runtime = Runtime.from_snapshot(load_package_snapshot(path))
    yield opened(runtime)
    runtime.close()


def _error(runtime: Runtime, call: dict[str, Any]) -> dict[str, Any]:
    result = runtime.validate(call, now=NOW)
    assert result["ok"] is False, result
    return result["errors"][0]


def _hint(error: dict[str, Any]) -> str:
    (hint,) = error["recovery_hints"]
    return str(hint["message"])


@pytest.mark.parametrize(
    ("surface", "code", "says"),
    [
        ("sdk", "INVALID_QUERY", "`context` argument"),
        ("mcp_stdio", "INVALID_QUERY", "configured credentials"),
        ("mcp_remote", "IDENTITY_CLAIM_REFUSED", "from the credentials"),
    ],
)
def test_an_identity_claim_names_where_the_surface_reads_identity(
    runtime: Runtime, surface: str, code: str, says: str
) -> None:
    claim = {"roles": ["admin"]}
    if surface == "sdk":
        error = _error(runtime, {"select": [ORDERS], "policy_context": claim})
    else:
        # The remote caller's environment is one the package does not declare.
        context = RequestContext(environment="trusted") if surface == "mcp_remote" else None
        request = {"query": {"select": [ORDERS]}, "policy_context": claim}
        adapter = SemanticLayerMCPAdapter(runtime)
        error = adapter.call_tool("execute", request, request_context=context)["errors"][0]
        assert "`context`" not in _hint(error)
    assert (error["code"], error["details"]["path"]) == (code, "policy_context")
    assert says in _hint(error)


@pytest.mark.parametrize(
    ("expression", "kind", "shape"),
    [
        ({"kind": "ratio", "numerator": 5}, "ratio", '"denominator": "..."'),
        ({"kind": "distribution", "function": "median", "over": ORDERS}, "distribution", '"over": "..."'),
        ({"kind": "case", "whens": "x"}, "case", '"whens": "..."'),
    ],
)  # fmt: skip
def test_a_malformed_expression_names_its_kind_and_path(
    runtime: Runtime, expression: dict[str, Any], kind: str, shape: str
) -> None:
    error = _error(runtime, {"select": [{"as": "x", "expression": expression}]})
    assert error["code"] == "INVALID_EXPRESSION_AST"
    assert error["details"]["expression_kind"] == kind
    assert error["details"]["path"] == "select[0].expression"
    hint = _hint(error)
    assert "the expression at select[0].expression" in hint and shape in hint
    assert "''" not in hint


def test_a_malformed_expression_keeps_its_own_hint(runtime: Runtime) -> None:
    rolling = {"kind": "rolling", "input": ORDERS, "window": "7d"}
    error = _error(runtime, {"select": [{"as": "x", "expression": rolling}], "time": {"grain": "day"}})  # fmt: skip
    assert error["recovery_hints"][0]["code"] == "USE_OBJECT_SHAPE"
    assert len(error["recovery_hints"]) == 1


def test_a_grain_the_call_does_not_know_names_the_grains(runtime: Runtime) -> None:
    offered = ["day", "week", "month", "quarter", "year"]
    for grain in ("hour", "fortnight"):
        error = _error(runtime, {"select": [ORDERS], "time": {"grain": grain}})
        assert (error["code"], error["details"]["path"]) == ("INVALID_QUERY", "time.grain")
        assert [unit for unit in ["hour", *offered] if unit in _hint(error)] == offered
    # Each option the hint offers validates: no grain (one total), or a named grain.
    for time in [None, *({"grain": unit} for unit in offered)]:
        call = {"select": [ORDERS], **({"time": time} if time else {})}
        assert runtime.validate(call, now=NOW)["ok"] is True


@pytest.mark.parametrize(
    "item",
    [
        {"child": "entity.jaffle_item", "match": "none", "where": [{"field": "dimension.jaffle_product_type", "op": "is", "value": "jaffle"}]},
        {"entity": "entity.jaffle_item", "where": [{"field": "dimension.jaffle_product_type", "op": "is", "value": "jaffle"}]},
    ],
    ids=["child-match-where", "entity-where"],
)  # fmt: skip
def test_a_child_group_is_pointed_at_the_entity_filter(
    runtime: Runtime, item: dict[str, Any]
) -> None:
    error = _error(runtime, {"select": [ORDERS], "where": [item]})
    assert error["details"]["path"] == "where[0]"
    hint = _hint(error)
    shape = json.loads(hint.removeprefix("Write ").removesuffix("."))
    word = item.get("match", "any")
    assert shape == {"entity": "entity.jaffle_item", word: item["where"]}
    assert runtime.validate({"select": [ORDERS], "where": [shape]}, now=NOW)["ok"] is True


# The largest window before the calendar's first day, counted back from NOW.
@pytest.mark.parametrize(
    ("unit", "value", "most"),
    [("year", 10000, 2016), ("day", 10**8, 736575), ("month", 10**30, 24200)],
)
def test_a_last_window_before_the_calendar_names_the_largest_one(
    runtime: Runtime, unit: str, value: int, most: int
) -> None:
    def last(count: int) -> dict[str, Any]:
        return {"select": [ORDERS], "time": {"last": {"unit": unit, "value": count}}}

    error = _error(runtime, last(value))
    assert error["details"]["path"] == "time.last.value"
    assert int(re.search(r"at most (\d+)", _hint(error)).group(1)) == most
    assert runtime.validate(last(most), now=NOW)["ok"] is True
    assert _error(runtime, last(most + 1))["details"]["path"] == "time.last.value"


@pytest.mark.parametrize(
    "time",
    [
        {"this": "year", "grain": "month"},
        {"this": "week"},
        {"start": "9999-12-01", "end": "9999-12-31", "grain": "week"},
    ],
)
def test_a_window_at_the_calendar_s_last_day_answers(
    runtime: Runtime, time: dict[str, Any]
) -> None:
    result = runtime.query({"select": [ORDERS], "time": time}, now="9999-12-31T12:00:00Z")
    assert result["ok"] is True
    assert all(row[ORDERS.split(".")[-1]] is None for row in result["rows"])


def test_an_unsettled_empty_group_names_the_callers_output(runtime: Runtime) -> None:
    """A where beside a per-period entity filter can't be judged under the dataset scope."""
    call = {
        "select": [{"as": "orders", "expression": ORDERS}],
        "where": [
            {"field": "dimension.jaffle_store_name", "op": "is", "value": "Brooklyn"},
            {"entity": "entity.jaffle_customer", "having": [{"field": ORDERS, "op": "gt", "value": 3}], "over": "period"},
        ],
        "time": {"grain": "month", "end": "2016-11-01"},
    }  # fmt: skip
    error = _error(runtime, call)
    assert error["code"] == "EMPTY_GROUPS_UNSETTLED"
    assert error["details"]["outputs"] == ["orders"]
    assert "leaf__" not in json.dumps(error)
    assert runtime.validate({**call, "observation_scope": "query"}, now=NOW)["ok"] is True


STORE = "dimension.jaffle_store_name"
CLOCK = "temporal_role.jaffle_order_time"
AOV = "metric.sales.aov_usd"


def _fragment(hint: str, key: str) -> Any:
    """The JSON the hint gives for ``key``: ``... "<key>": <json>. ...``."""
    start = hint.index(f'"{key}": ') + len(key) + 4
    return json.JSONDecoder().raw_decode(hint, start)[0]


def _predicate(scope: str, threshold: int = 2) -> dict[str, Any]:
    return {
        "kind": "metric_predicate",
        "entity": "entity.jaffle_customer",
        "input": {"kind": "measure", "measure": ORDERS},
        "op": ">",
        "value": threshold,
        "scope_mode": scope,
    }


@pytest.mark.parametrize(
    ("select", "item", "part"),
    [
        ([{"expression": {"kind": "metric", "metric": AOV}, "as": "aov"}], {"expression": {"metric": AOV}, "op": ">", "value": 12.5}, "having"),
        ([REVENUE], {"expression": {"kind": "measure", "measure": REVENUE}, "op": "<=", "value": 300000}, "having"),
        ([ORDERS], {"expression": _predicate("entity_only"), "op": "=", "value": True}, "where"),
    ],
    ids=["metric-alias", "measure-id", "entity-predicate"],
)  # fmt: skip
def test_retired_result_filters_hint_the_callers_own_rows(
    runtime: Runtime, select: list[Any], item: dict[str, Any], part: str
) -> None:
    error = _error(runtime, {"select": select, "group_by": [STORE], "metric_filters": [item]})
    assert error["details"]["path"] == "metric_filters"
    rows = _fragment(_hint(error), part)
    fixed = {"select": [_v2_item(row) for row in select], "group_by": [STORE], part: rows}
    # The rewrite answers what the engine reads the retired filter as.
    retired = runtime.engine_payload({**fixed, part: []}, now=NOW)
    retired["metric_filters"] = [item]
    expected = runtime.query_internal(retired)["rows"]
    unfiltered = runtime.query({**fixed, part: []}, now=NOW)["rows"]
    assert runtime.query(fixed, now=NOW)["rows"] == expected
    assert expected and expected != unfiltered


def _v2_item(row: Any) -> Any:
    if isinstance(row, dict) and isinstance(row["expression"], dict):
        return {**row, "expression": row["expression"]["metric"]}
    return row


def test_a_result_filter_with_no_mechanical_rewrite_keeps_the_general_hint(
    runtime: Runtime,
) -> None:
    items = [
        {"expression": {"kind": "measure", "measure": ORDERS}, "op": ">", "value": 10},
        {"expression": _predicate("contextual"), "op": "=", "value": True},
    ]
    error = _error(runtime, {"select": [ORDERS], "group_by": [STORE], "metric_filters": items})
    hint = _hint(error)
    assert _fragment(hint, "having") == [{"field": "order_count", "op": "gt", "value": 10}]
    assert '"where"' not in hint
    assert "For metric_filters[1]: Filter results with `having" in hint


@pytest.mark.parametrize(
    ("call", "path", "hint", "fixed"),
    [
        (
            {"select": [ORDERS], "time": {"temporal_role": CLOCK, "grain": "month", "range": {"last": {"unit": "day", "value": 90}}, "calendar_id": "default"}},
            "time.range",
            'Write "time": {"temporal_role": "' + CLOCK + '", "grain": "month", "last": {"unit": "day", "value": 90}}.',
            {"select": [ORDERS], "time": {"temporal_role": CLOCK, "grain": "month", "last": {"unit": "day", "value": 90}}},
        ),
        (
            {"select": [ORDERS], "time": {"range": {"start": "2017-01-01", "end": "2017-03-01"}}},
            "time.range",
            'Write "time": {"start": "2017-01-01", "end": "2017-03-01"}.',
            {"select": [ORDERS], "time": {"start": "2017-01-01", "end": "2017-03-01"}},
        ),
        (
            {"select": [ORDERS], "time": {"grain": "month"}, "order_by": [{"field": CLOCK + "__month", "direction": "desc"}]},
            "order_by[0].field",
            'Write "period".',
            {"select": [ORDERS], "time": {"grain": "month"}, "order_by": [{"field": "period", "direction": "desc"}]},
        ),
        (
            {"select": [{"metric": AOV, "as": "aov"}]},
            "select[0]",
            f'Write {{"expression": "{AOV}", "as": "aov"}}.',
            {"select": [{"expression": AOV, "as": "aov"}]},
        ),
        (
            {"select": [{"expression": {"kind": "measure_ref", "measure": REVENUE, "aggregation": "max"}, "as": "top"}]},
            "select[0].expression",
            f'Write {{"kind": "measure", "measure": "{REVENUE}", "aggregation": "max"}}.',
            {"select": [{"expression": {"kind": "measure", "measure": REVENUE, "aggregation": "max"}, "as": "top"}]},
        ),
        (
            {"select": [{"expression": {"kind": "prior_period", "measure": ORDERS, "offset": -2, "grain": "month"}, "as": "before"}], "time": {"grain": "month"}},
            "select[0].expression",
            f'Write {{"kind": "prior_period", "input": "{ORDERS}", "offset": {{"unit": "month", "value": 2}}}}.',
            {"select": [{"expression": {"kind": "prior_period", "input": ORDERS, "offset": {"unit": "month", "value": 2}}, "as": "before"}], "time": {"grain": "month"}},
        ),
        (
            {"select": [{"expression": {"kind": "scoped_aggregate", "measure": ORDERS, "predicates": [{**_predicate("entity_only"), "time_alignment": "query_window"}]}, "as": "n"}], "time": {"last": {"unit": "year", "value": 1}}},
            "select[0].expression.predicates[0].scope_mode",
            'Replace scope_mode and time_alignment with "over": "window".',
            None,
        ),
        (
            {"select": [ORDERS], "where": [{"field": STORE, "op": "LIKE", "value": "%Phil%"}]},
            "where[0].op",
            'Write {"op": "contains", "value": "Phil"}, which ignores case.',
            {"select": [ORDERS], "where": [{"field": STORE, "op": "contains", "value": "Phil"}]},
        ),
        (
            {"select": [ORDERS], "where": [{"field": STORE, "op": "IS DISTINCT FROM", "value": "Philadelphia"}]},
            "where[0].op",
            'Write {"op": "not", "value": "Philadelphia"}.',
            {"select": [ORDERS], "where": [{"field": STORE, "op": "not", "value": "Philadelphia"}]},
        ),
        (
            {"select": [ORDERS], "where": [{"field": "dimension.jaffle_order_total_usd", "op": "BETWEEN", "value": [5, 10]}]},
            "where[0].op",
            'Write two conditions, {"op": "gte", "value": 5} and {"op": "lte", "value": 10}.',
            None,
        ),
        (
            {"select": [ORDERS], "route_decisions": [{"source_entity": "entity.jaffle_order", "target_entity": "entity.jaffle_store", "relationship_path": ["relationship.order_store"]}]},
            "route_decisions",
            'Write "routes": [{"from": "entity.jaffle_order", "to": "entity.jaffle_store", "via": ["relationship.order_store"]}].',
            None,
        ),
        (
            {"select": [ORDERS], "limits": {"max_rows": 50}},
            "limits",
            "Pass `max_rows: 50` as a tool argument.",
            None,
        ),
    ],
    ids=["range-last", "range-dates", "axis-alias", "metric-object", "measure-ref", "prior-period", "predicate-scope", "like", "distinct-from", "between", "route-decisions", "limits"],
)  # fmt: skip
def test_a_retired_spelling_is_hinted_with_the_callers_values(
    runtime: Runtime,
    call: dict[str, Any],
    path: str,
    hint: str,
    fixed: dict[str, Any] | None,
) -> None:
    error = _error(runtime, call)
    assert error["details"]["path"] == path
    assert _hint(error).removesuffix(f" {V2_POINTER}") == hint
    if fixed is not None:
        assert runtime.validate(fixed, now=NOW)["ok"] is True


TOP_REVENUE = {
    "expression": {"kind": "measure", "measure": REVENUE, "aggregation": "max"},
    "as": "top",
}


@pytest.mark.parametrize(
    ("select", "field", "column"),
    [
        ([ORDERS, REVENUE], REVENUE, "revenue_usd"),
        ([ORDERS, {"expression": REVENUE, "as": "revenue"}], REVENUE, "revenue"),
        ([ORDERS, {"expression": {"kind": "measure", "measure": REVENUE}, "as": "revenue"}], REVENUE, "revenue"),
        # The measure's own aggregation, written out, is no override: the same number.
        ([ORDERS, {"expression": {"kind": "measure", "measure": REVENUE, "aggregation": "sum"}, "as": "revenue"}], REVENUE, "revenue"),
        # Beside another statistic of it, the id is the item that reads it as itself.
        ([TOP_REVENUE, REVENUE], REVENUE, "revenue_usd"),
        ([ORDERS, AOV], AOV, "aov_usd"),
    ],
    ids=["measure", "named-measure", "measure-kind", "default-statistic", "beside-a-statistic-override", "metric"],
)  # fmt: skip
def test_order_by_a_selected_id_orders_by_its_column(
    runtime: Runtime, select: list[Any], field: str, column: str
) -> None:
    call = {"select": select, "group_by": [STORE], "limit": 1}
    by_id = {**call, "order_by": [{"field": field, "direction": "desc"}]}
    by_column = {**call, "order_by": [{"field": column, "direction": "desc"}]}
    assert runtime.engine_payload(by_id, now=NOW)["order_by"] == [{"field": column, "direction": "DESC"}]  # fmt: skip
    assert runtime.query(by_id, now=NOW)["rows"] == runtime.query(by_column, now=NOW)["rows"]


@pytest.mark.parametrize(
    ("select", "columns"),
    [
        # Two columns read it as itself: which one orders is the caller's to say.
        ([REVENUE, {"expression": REVENUE, "as": "revenue"}], ["revenue_usd", "revenue"]),
        # Only another statistic reads it: the id names a number no column holds.
        ([ORDERS, TOP_REVENUE], ["top"]),
    ],
    ids=["selected-twice", "only-a-statistic-override"],
)  # fmt: skip
def test_order_by_an_id_no_one_column_reads_names_its_columns(
    runtime: Runtime, select: list[Any], columns: list[str]
) -> None:
    call = {"select": select, "group_by": [STORE], "order_by": [{"field": REVENUE}]}
    error = _error(runtime, call)
    assert (error["code"], error["details"]["path"]) == ("INVALID_ORDER_BY", "order_by[0].field")
    assert error["details"]["closest_matches"] == columns
    assert all(column in _hint(error) for column in columns)
    for column in columns:
        assert runtime.validate({**call, "order_by": [{"field": column}]}, now=NOW)["ok"] is True
    # An id no select item reads is refused as before, with the columns the call can name.
    unselected = _error(runtime, {**call, "order_by": [{"field": AOV}]})
    assert unselected["code"] == "INVALID_ORDER_BY"
    assert "available_select_aliases" in unselected["details"]


STORE_IDS = [
    "dimension.jaffle_order_lifecycle_store_id",
    "dimension.jaffle_order_store_id",
    "dimension.jaffle_store_id",
    "dimension.jaffle_store_inventory_snapshot_store_id",
    "dimension.jaffle_storefront_session_store_id",
]


@pytest.mark.parametrize(
    ("column", "dimensions"),
    [
        ({"column": "store_name"}, [STORE]),
        ({"column": "store_id"}, STORE_IDS),
        ({"column": "store_id", "entity": "entity.jaffle_order"}, ["dimension.jaffle_order_store_id"]),
        ({"column": "store_id", "table": "jaffle_order"}, ["dimension.jaffle_order_store_id"]),
        ({"column": "store_name", "entity": "entity.jaffle_order"}, [STORE]),
        ({"column": "no_such_column"}, None),
    ],
    ids=["column", "column-on-many-entities", "entity", "table", "column-on-another-entity", "unknown-column"],
)  # fmt: skip
def test_a_raw_column_names_the_query_kinds_and_its_dimensions(
    runtime: Runtime, column: dict[str, str], dimensions: list[str] | None
) -> None:
    select = [ORDERS, {"expression": {"kind": "column", **column}, "as": "x"}]
    error = _error(runtime, {"select": select})
    assert (error["code"], error["message"]) == ("INVALID_QUERY", "Unsupported query expression kind 'column'")  # fmt: skip
    assert error["details"]["path"] == "select[1].expression"
    assert error["details"].get("closest_matches") == dimensions
    kinds = error["details"]["supported_kinds"]
    # Every kind of the call is offered but those a select item cannot be.
    assert set(call_kinds()) - set(kinds) == {"column", "in", "not_in", "date_add", "metric_predicate"}  # fmt: skip
    hint = _hint(error)
    assert all(f"'{kind}'" in hint for kind in kinds) and "group_by" in hint
    if dimensions and len(dimensions) == 1:
        # The one dimension on the column splits a measure in its place.
        assert runtime.validate({"select": [ORDERS], "group_by": dimensions}, now=NOW)["ok"] is True


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ({"column": "order_total_cents", "entity": "entity.jaffle_order"}, 74_589_303),
        ({"column": "order_total_cents", "table": "jaffle_order"}, 74_589_303),
        ({"column": "order_total_cents"}, None),
    ],
    ids=["entity", "table", "no-entity"],
)
def test_a_raw_column_hint_writes_the_aggregate_if_that_aggregates_it(
    runtime: Runtime, column: dict[str, str], value: int | None
) -> None:
    """Total order value straight from its column: the hint writes the aggregate_if over that
    column, which answers, instead of saying a query reads only measures and metrics."""
    select = [{"expression": {"kind": "column", **column}, "as": "total"}]
    hint = _hint(_error(runtime, {"select": select}))
    owner = {key: column[key] for key in ("entity", "table") if key in column}
    shape = {
        "kind": "aggregate_if",
        "aggregation": "sum",
        "value": {"kind": "column", "column": "order_total_cents", **(owner or {"entity": "<entity id>"})},
    }  # fmt: skip
    assert json.dumps(shape) in hint
    assert "measures and metrics" not in hint and "LENGTH" in hint and "group_by" in hint
    if value is not None:
        answer = runtime.query({"select": [{"expression": shape, "as": "total"}]}, now=NOW)
        assert answer["rows"] == [{"total": value}]


@pytest.mark.parametrize("withheld", [False, True], ids=["denied", "withheld"])
def test_a_raw_column_hint_offers_no_aggregate_over_a_kept_measures_column(
    runtime: Runtime, withheld: bool
) -> None:
    """Revenue denied to partners, or withheld from support, is the measure only, not its
    column: their raw-column hint offers no aggregate over order_total_cents, while a caller
    the policy doesn't name is still offered one."""
    engine, caller = runtime, RequestContext(audience="external_partner")
    if withheld:
        config = runtime.package_config
        policy = SemanticPolicyConfig(
            id="policy.test.withhold_revenue",
            kind="object_access",
            action="withhold_values",
            object_ids=[REVENUE],
            roles=["support"],
            config={"max_rank": 3},
        )
        engine = Runtime.from_config(
            replace(config, semantic_policies=[*config.semantic_policies, policy]),
            source_path="configs/semantic_rails/jaffle_shop",
        )
        caller = RequestContext(roles=("support",))
    column = {"kind": "column", "column": "order_total_cents", "entity": "entity.jaffle_order"}
    call = {"select": [{"expression": column, "as": "total"}]}
    try:
        (error,) = engine.validate(call, now=NOW, context=caller)["errors"]
        offered = _hint(_error(engine, call))
    finally:
        if engine is not runtime:
            engine.close()
    hint = _hint(error)
    assert error["code"] == "INVALID_QUERY"
    assert '"kind": "aggregate_if"' not in hint and "order_total_cents" not in hint
    # The hint is the policy's only trace: no detail says a policy keeps the column.
    assert "kept_by_policy" not in json.dumps(error) and "recovery_hints" not in error["details"]
    assert hint.endswith(
        "To split or filter by a column, name a dimension on it in group_by or where."
    )
    assert '{"kind": "aggregate_if", "aggregation": "sum", "value": {"kind": "column", "column": "order_total_cents"' in offered  # fmt: skip


def test_a_raw_column_in_a_draft_names_its_path() -> None:
    draft = {"select": [{"expression": {"kind": "column", "column": "store_id"}, "as": "x"}]}
    with pytest.raises(SemanticLayerError) as raised:
        normalize_partial_query(draft)
    assert raised.value.details["path"] == "select[0].expression"


def test_the_dimensions_on_a_column_are_capped(package_config_factory) -> None:
    config, _ = package_config_factory("jaffle_shop")
    store = next(row for row in config.dimensions if row.id == STORE)
    copies = [replace(store, id=f"dimension.store_name_{n:02d}") for n in range(12)]
    details = {"expression_kind": "column", "column": "store_name"}
    error = SemanticLayerError("INVALID_QUERY", "Unsupported query expression kind 'column'", details=details)  # fmt: skip
    enriched = enrich_column_expression(error, replace(config, dimensions=copies))
    assert enriched.details["closest_matches"] == [row.id for row in copies[:10]]


ORDER = "entity.jaffle_order"
CUSTOMER = "entity.jaffle_customer"
HISTORY = "entity.jaffle_customer_history"


@pytest.mark.parametrize(
    ("route", "relationships"),
    [
        ({"from": ORDER, "to": CUSTOMER, "via": ["customer_id"]}, ["relationship.orders_customer"]),
        # No route joins a calendar to a customer: the relationships touching either entity.
        (
            {"from": "entity.jaffle_fiscal_calendar", "to": CUSTOMER, "via": [ORDER, CUSTOMER]},
            [
                "relationship.customer_history_customer",
                "relationship.order_lifecycle_customer",
                "relationship.orders_customer",
                "relationship.storefront_sessions_customer",
            ],
        ),
    ],
    ids=["column-joining-the-pair", "entity-ids-no-route"],
)  # fmt: skip
def test_a_route_via_that_names_no_relationship_lists_the_relationships_at_its_pair(
    runtime: Runtime, route: dict[str, Any], relationships: list[str]
) -> None:
    call = {"select": [ORDERS], "group_by": ["dimension.jaffle_customer_name"], "routes": [route]}
    error = _error(runtime, call)
    assert (error["code"], error["details"]["path"]) == ("INVALID_QUERY", "routes[0]")
    assert error["details"]["reason"] == "invalid_route_decision"
    assert error["details"]["closest_matches"] == relationships
    assert "other_routes" not in error["details"]
    assert "relationship ids" in _hint(error)


SEGMENTS = {
    "select": ["measure.jaffle.customer_count"],
    "group_by": ["dimension.jaffle_customer_history_segment"],
    "time": {"grain": "year"},
}


def test_a_route_via_with_no_one_hop_join_offers_the_pair_s_routes(runtime: Runtime) -> None:
    call = {**SEGMENTS, "routes": [{"from": CUSTOMER, "to": HISTORY, "via": ["customer_id"]}]}
    error = _error(runtime, call)
    assert (error["code"], error["details"]["path"]) == ("INVALID_QUERY", "routes[0]")
    # The one relationship joining the pair walks only from the history to the customer.
    assert "closest_matches" not in error["details"]
    routes = error["details"]["other_routes"]
    assert routes == [
        {"from": CUSTOMER, "to": HISTORY, "via": ["relationship.orders_customer", "relationship.jaffle_order_customer_history"]},
        {"from": CUSTOMER, "to": HISTORY, "via": ["relationship.storefront_sessions_customer", "relationship.storefront_sessions_order", "relationship.jaffle_order_customer_history"]},
    ]  # fmt: skip
    assert "more_routes" not in error["details"]
    assert "relationship ids" in _hint(error)
    for route in routes:
        assert runtime.validate({**SEGMENTS, "routes": [route]}, now=NOW)["ok"] is True


def test_the_relationships_touching_a_pair_are_capped(monkeypatch) -> None:
    monkeypatch.setattr(binding, "pair_routes", lambda *args: [])
    touching = [
        SimpleNamespace(id=f"relationship.r{n:02d}", source_entity=ORDER, target_entity=f"entity.e{n}")
        for n in range(12)
    ]  # fmt: skip
    details = binding._via_candidates(SimpleNamespace(relationships=touching), ORDER, CUSTOMER)
    assert details["closest_matches"] == [row.id for row in touching[:10]]


# The modules that write refusal messages, hints and answer disclosures, and the call words
# that left the call. A word is matched in prose (a string literal with a space; docstrings are
# not emitted); ALLOWED lists the phrases where one means something else.
WRITERS = (
    "issues.py",
    "diagnostics.py",
    "query_call.py",
    "policies.py",
    "answer.py",
    "runtime.py",
    "runtime_parts/assumptions.py",
    "runtime_parts/answer_warnings.py",
    "metadata_parts/valid_values.py",
)
RETIRED_WORDS = {
    "metric_filters": r"\bmetric_filters?\b",
    "metric-filter": r"\bmetric-filters?\b",
    "temporal as a cut": r"\btemporal\b",
    "range": r"\brange\b",
    "fill": r"\bfill\b",
    "version": r"\bversion\b",
    "policy_context": r"\bpolicy_context\b",
    "time_dimension": r"\btime_dimensions?\b",
    "grain as a top-level key": r"(?<![\w.])[`'\"]grain[`'\"]|(?<![\w.`'\"])grain\s*:",
}
ALLOWED = ("temporal role", "range predicate", "time.fill")


def _prose(path: Path) -> Iterator[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skipped = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            skipped.update(id(part) for part in node.values)
            text = "".join(p.value if isinstance(p, ast.Constant) else "{}" for p in node.values)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in skipped:
                continue
            text = node.value
        else:
            continue
        if " " in text.strip():
            yield node.lineno, text


def test_no_refusal_hint_or_disclosure_uses_a_retired_call_word() -> None:
    root = Path(semantic_rails.__file__).parent
    hits = []
    for name in WRITERS:
        for line, text in _prose(root / name):
            prose = text
            for phrase in ALLOWED:
                prose = prose.replace(phrase, "")
            hits += [
                f"{name}:{line} [{word}] {text[:80]!r}"
                for word, pattern in RETIRED_WORDS.items()
                if re.search(pattern, prose)
            ]
    assert hits == []
