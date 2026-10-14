"""Refusals and hints say where the problem is and what to send instead.

A dimension where a measure goes says it is a dimension and names the distinct counts of its
column. PATH_NOT_FOUND names the query's own pair and how its start was chosen, says what
blocked an isolated start and what to drop or split, and never asks for a relationship into a
row-grain model. RESULT_TOO_LARGE names the largest field and a concrete retry. execute refuses
a query key passed beside query. FILTER_VALUE_NOT_FOUND stays small and suggests no weak or
id-like match. A date filter value that can't be read, an unknown arithmetic op in a package
metric and an item after a lifted dimension are refused at their own path. An unresolved
"current" names the package's current-state flags and snapshots. A safe rewrite is a note.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from semantic_rails.diagnostics import enrich_path_not_found, exception_issue, filter_value_miss
from semantic_rails.errors import SemanticLayerError
from semantic_rails.fanout import resolve_path
from semantic_rails.mcp import SemanticLayerMCPAdapter, list_tool_definitions
from semantic_rails.runtime import Runtime
from semantic_rails.schema import DimensionConfig, EntityConfig, RelationshipConfig
from tests.semantic_rails.conftest import opened, write_single_file_package
from tests.semantic_rails.test_runtime_and_metadata import jaffle_snapshot as jaffle_snapshot

ORDERS = "measure.jaffle.order_count"
STORE = "dimension.jaffle_store_name"
MEMBERSHIP = "dimension.jaffle_membership_status"


@pytest.fixture(scope="module")
def runtime(jaffle_snapshot):
    runtime = opened(Runtime.from_snapshot(jaffle_snapshot, package_id="jaffle_shop"))
    yield runtime
    runtime.close()


@pytest.fixture(scope="module")
def adapter(runtime):
    return SemanticLayerMCPAdapter(runtime)


def _error(runtime: Runtime, call: dict[str, Any]) -> dict[str, Any]:
    result = runtime.validate(call)
    assert result["ok"] is False, result
    return result["errors"][0]


def _hints(error: dict[str, Any]) -> str:
    return " ".join(str(hint.get("message", "")) for hint in error.get("recovery_hints") or [])


# --- A dimension where a measure goes ---------------------------------------------------------


def test_a_dimension_counted_as_a_measure_names_the_distinct_counts_of_its_column(runtime):
    expression = {
        "kind": "measure",
        "measure": "dimension.jaffle_customer_id",
        "aggregation": "count_distinct",
    }
    error = _error(runtime, {"select": [{"expression": expression, "as": "n"}]})
    assert error["code"] == "INVALID_QUERY"
    assert error["details"]["path"] == "select[0].expression.measure"
    assert "is a dimension" in error["message"]
    assert error["details"]["closest_matches"] == ["measure.jaffle.customer_count"]
    assert "measure.jaffle.customer_count" in _hints(error)
    assert "group_by" in _hints(error)


@pytest.mark.parametrize(
    ("expression", "path"),
    [
        (
            {"kind": "aggregate", "measure": STORE, "aggregation": "count_distinct"},
            "select[0].expression.measure",
        ),
        (
            {"kind": "ratio", "numerator": STORE, "denominator": ORDERS},
            "select[0].expression.numerator",
        ),
        (
            {"kind": "prior_period", "input": STORE, "offset": {"unit": "month", "value": 1}},
            "select[0].expression.input",
        ),
    ],
    ids=["aggregate", "ratio-operand", "prior-period-input"],
)
def test_a_dimension_in_a_measure_slot_says_to_group_by_it(runtime, expression, path):
    call = {"select": [{"expression": expression, "as": "n"}]}
    if expression["kind"] == "prior_period":
        call["time"] = {"grain": "month"}
    error = _error(runtime, call)
    assert error["details"]["path"] == path
    assert f"'{STORE}' is a dimension" in error["message"]
    assert error["details"]["closest_matches"] == []
    assert "group_by" in _hints(error)


# --- PATH_NOT_FOUND ----------------------------------------------------------------------------


def test_a_query_without_a_measure_names_its_own_pair_and_how_it_started(runtime):
    error = _error(runtime, {"group_by": [STORE, MEMBERSHIP]})
    assert error["code"] == "PATH_NOT_FOUND"
    details = error["details"]
    assert details["start"] == "entity.jaffle_store"
    assert details["target"] == "entity.jaffle_customer_segment_membership"
    assert "entity.jaffle_customer'" not in json.dumps(error)
    assert "without a measure" in details["anchor"]
    assert details["unreachable_items"] == [MEMBERSHIP]
    assert MEMBERSHIP in _hints(error)


def test_a_measure_query_says_its_start_is_the_measures_entity(runtime):
    error = _error(runtime, {"select": ["measure.jaffle.revenue_usd"], "group_by": [MEMBERSHIP]})
    assert error["code"] == "PATH_NOT_FOUND"
    assert "measure.jaffle.revenue_usd" in error["details"]["anchor"]
    assert error["details"]["unreachable_items"] == [MEMBERSHIP]


def _isolated(package_config_factory, *, target_key: list[str]) -> Any:
    config, _ = package_config_factory("jaffle_shop")
    keys = {"S": ["s_id"], "B": ["b_id"], "T": target_key}
    return replace(
        config,
        entities=[
            EntityConfig(id=node, table=node.lower(), key_columns=keys[node]) for node in "SBT"
        ],
        dimensions=[
            DimensionConfig(id="dimension.s_id", entity="S", column="s_id", data_type="id"),
            DimensionConfig(id="dimension.s_t", entity="S", column="t_id", data_type="id"),
            DimensionConfig(id="dimension.t_name", entity="T", column="name", data_type="string"),
        ],
        relationships=[
            # S's only relationship may be walked toward S, never from it.
            RelationshipConfig(
                id="S_B",
                source_entity="S",
                target_entity="B",
                source_column="b_id",
                target_column="b_id",
                cardinality="N:1",
                safety="safe",
                allowed_directions=["reverse"],
            ),
            RelationshipConfig(
                id="T_B",
                source_entity="T",
                target_entity="B",
                source_column="b_id",
                target_column="b_id",
                cardinality="N:1",
                safety="safe",
            ),
        ],
        path_preferences=[],
    )


def _path_issue(config: Any) -> dict[str, Any]:
    with pytest.raises(SemanticLayerError) as raised:
        resolve_path(config, start="S", target="T")
    return exception_issue(enrich_path_not_found(raised.value, config), stage="validate")


def test_an_isolated_start_names_bridges_shared_keys_the_block_and_a_workaround(
    package_config_factory,
):
    issue = _path_issue(_isolated(package_config_factory, target_key=["t_id"]))
    details = issue["details"]
    assert details["reachable_targets"] == []
    assert details["blocked_by"] == "direction"
    assert details["bridge_entities"] == ["B"]
    assert details["shared_key_columns"] == ["t_id"]
    hints = _hints(issue)
    assert "direction" in hints and "'B'" in hints and "t_id" in hints
    assert "split the query" in hints.lower()


def test_a_row_grain_target_never_gets_a_relationship_into_it(package_config_factory):
    issue = _path_issue(_isolated(package_config_factory, target_key=[]))
    assert issue["details"]["target_row_grain"] is True
    assert all(hint["kind"] != "author_relationship" for hint in issue["recovery_hints"])
    assert "author a relationship" not in _hints(issue).lower()
    assert "row-grain" in _hints(issue)


# --- RESULT_TOO_LARGE --------------------------------------------------------------------------


def test_an_oversized_answer_names_its_largest_field_and_a_retry(adapter, monkeypatch):
    rows = [{"store": f"s{i}", "note": "x" * 2_000} for i in range(40)]
    monkeypatch.setattr(
        adapter.runtime, "query", lambda *_a, **_k: {"ok": True, "row_count": 40, "rows": rows}
    )
    response = adapter.call_tool("execute", {"query": {"select": [ORDERS]}})
    error = response["errors"][0]
    assert error["code"] == "RESULT_TOO_LARGE"
    assert error["details"]["largest_field"] == "note"
    assert error["details"]["retry"] == {"row_format": "columns", "sql_profile": "off"}
    assert "'note'" in error["message"]
    assert "row_format" in error["message"] and "sql_profile" in error["message"]


# --- execute's arguments -----------------------------------------------------------------------


@pytest.mark.parametrize("key", ["limit", "group_by"])
def test_a_query_key_beside_query_is_refused_with_where_it_goes(adapter, key):
    value: Any = 3 if key == "limit" else [STORE]
    response = adapter.call_tool("execute", {"query": {"select": [ORDERS]}, key: value})
    assert response["ok"] is False
    error = response["errors"][0]
    assert error["details"]["path"] == key
    assert "inside `query`" in _hints(error)
    if key == "limit":
        assert "max_rows" in _hints(error)


def test_execute_says_where_its_arguments_go_and_to_run_directly():
    (execute,) = [tool for tool in list_tool_definitions() if tool["name"] == "execute"]
    description = execute["description"]
    assert "max_rows and row_format go beside query" in description
    assert "limit inside it" in description
    assert "run it directly" in description


# --- FILTER_VALUE_NOT_FOUND --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("literal", "values", "data_type", "suggestion"),
    [
        ("brooklin", ["Brooklyn", "Philadelphia"], "string", "Brooklyn"),
        ("Stata", ["WESTGATE", "EASTGATE"], "string", None),
        ("ORD-1235", ["ORD-1234"], "string", None),
        ("a1b2c3d4e5", ["a1b2c3d4e6"], "string", None),
        ("brooklyn", ["Brooklyn"], "id", "Brooklyn"),
        ("brookln", ["Brooklyn"], "id", None),
        ("store 12", ["Store 12"], "string", "Store 12"),
    ],
    ids=["typo", "weak", "code", "hash", "id-case", "id-typo", "case-with-digits"],
)
def test_a_missing_filter_value_suggests_only_a_strong_match(
    literal, values, data_type, suggestion
):
    _, suggested = filter_value_miss("This query", "dimension.x", literal, values, data_type)
    assert suggested == suggestion


def test_many_missing_filter_values_keep_the_warning_small(runtime):
    missing = [f"Nowhere {index}" for index in range(30)]
    result = runtime.query(
        {"select": [ORDERS], "where": [{"field": STORE, "op": "is", "value": missing}]}
    )
    (warning,) = [w for w in result["warnings"] if w["code"] == "FILTER_VALUE_NOT_FOUND"]
    assert len(json.dumps(warning)) < 2_000
    assert warning["details"]["missing_count"] == 30
    assert len(warning["details"]["filters"]) <= 10
    assert "25 more" in warning["message"]


# --- Refusals at their own path ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dimension.jaffle_order_ordered_at", "abc"),
        ("dimension.jaffle_customer_first_order_at", "2017-13-45"),
    ],
)
def test_a_date_filter_value_that_cant_be_read_is_refused_at_its_value(runtime, field, value):
    error = _error(
        runtime, {"select": [ORDERS], "where": [{"field": field, "op": "is", "value": value}]}
    )
    assert error["code"] == "INVALID_QUERY"
    assert error["details"]["path"] == "where[0].value"
    assert field in error["message"] and repr(value) in error["message"]


@pytest.mark.parametrize("value", ["2017-09-04", "2017-09-04T12:00:00", "2017-09-04 12:00:00"])
def test_a_date_filter_value_in_iso_form_still_reads(runtime, value):
    where = [{"field": "dimension.jaffle_order_ordered_at", "op": "gte", "value": value}]
    assert runtime.validate({"select": [ORDERS], "where": where})["ok"]


def test_an_unknown_arithmetic_op_in_a_package_metric_is_refused_at_its_path(tmp_path: Path):
    package = write_single_file_package(tmp_path / "project")
    text = package.read_text(encoding="utf-8")
    old = "      op: divide\n      left:  { kind: metric"
    assert old in text
    package.write_text(text.replace(old, "      op: div\n      left:  { kind: metric"))
    with pytest.raises(SemanticLayerError) as raised:
        Runtime.from_path(str(package.parent))
    message = str(raised.value)
    assert "revenue_per_item_usd" in message
    assert "'div'" in message and "add, subtract, multiply or divide" in message


def test_an_item_after_a_lifted_dimension_is_refused_at_the_callers_index(runtime):
    column = {"kind": "column", "column": "status"}
    error = _error(runtime, {"select": [STORE, ORDERS, {"expression": column, "as": "x"}]})
    assert error["details"]["path"] == "select[2].expression"
    ratio = {"kind": "ratio", "numerator": ORDERS}
    error = _error(runtime, {"select": [STORE, {"expression": ratio, "as": "x"}]})
    assert error["details"]["path"] == "select[1].expression"
    assert "select[1].expression" in _hints(error)


# --- "current" ---------------------------------------------------------------------------------


def test_an_unresolved_current_names_flags_and_snapshots(package_config_factory):
    from semantic_rails.planner.intent_holds import _unresolved_time_why

    config, _ = package_config_factory("jaffle_shop")
    flag = DimensionConfig(
        id="dimension.jaffle_customer_is_current",
        entity="entity.jaffle_customer",
        column="is_current",
        data_type="boolean",
    )
    config = replace(config, dimensions=[*config.dimensions, flag])
    why = _unresolved_time_why("current customer count", None, (), config=config)
    assert why is not None and why["code"] == "TIME_WINDOW_UNRESOLVED"
    hint = next(h for h in why["recovery_hints"] if h["kind"] == "use_current_state")
    assert flag.id in hint["message"]
    why = _unresolved_time_why("customers near current inventory", None, (), config=config)
    hint = next(h for h in why["recovery_hints"] if h["kind"] == "use_current_state")
    assert "measure.jaffle.inventory_on_hand_eop" in hint["message"]


def test_an_unresolved_current_without_a_state_offers_no_state_hint(package_config_factory):
    from semantic_rails.planner.intent_holds import _unresolved_time_why

    config, _ = package_config_factory("jaffle_shop")
    why = _unresolved_time_why("current customer count", None, (), config=config)
    assert why is not None
    assert all(hint["kind"] != "use_current_state" for hint in why["recovery_hints"])


# --- A safe rewrite is a note ------------------------------------------------------------------


def test_a_safe_cross_fact_rewrite_is_an_info_note(runtime):
    result = runtime.query(
        {
            "select": ["measure.jaffle.revenue_usd", "measure.jaffle.session_starts"],
            "group_by": [STORE],
        }
    )
    assert result["ok"]
    (rewrite,) = [w for w in result["warnings"] if w["code"] == "REWRITE_APPLIED"]
    assert rewrite["severity"] == "info"
    assert rewrite["message"].startswith("Safe cross-fact alignment")
