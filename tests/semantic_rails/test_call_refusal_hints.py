"""Each refusal names its path and one hint the caller can follow, in the call's own terms."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from semantic_rails.embedding import RequestContext
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.package_snapshot import load_package_snapshot
from semantic_rails.query_call import V2_POINTER
from semantic_rails.runtime import Runtime
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
