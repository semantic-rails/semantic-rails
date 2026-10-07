"""A ``withhold_values`` policy lets a caller rank by an object without seeing its values.

"The three biggest stores by revenue" is answered with the stores only. Every other use of
the withheld object (a selected expression, filter, threshold, comparison, segment or
export) is refused with ``POLICY_DENIED`` on every surface, and no response carries a value.
The numbers themselves are checked against reference SQL in the correctness suite.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

from semantic_rails.compiler import bind_query
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.mcp_server import _tool_content
from semantic_rails.metadata_parts.valid_values import valid_values_payload
from semantic_rails.policies import enforce_query_policies, withheld_measure_ids, withheld_shape
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig
from tests.semantic_rails.conftest import copy_package_config, opened

REVENUE = "measure.jaffle.revenue_usd"
LIFETIME = "measure.jaffle.lifetime_spend_usd"
AOV = "metric.sales.aov_usd"
CUSTOMERS = "metric.sales.customer_count"
STORE = "dimension.jaffle_store_name"
ROLE = "temporal_role.jaffle_order_time"
SALES = {"roles": ["sales"]}
RANK = {
    "select": [{"expression": {"measure": REVENUE}, "as": "revenue"}],
    "group_by": [STORE],
    "order_by": [{"field": "revenue", "direction": "DESC"}],
    "limit": 3,
    "policy_context": SALES,
}
MONTHLY = {"time": {"temporal_role": ROLE, "grain": "month"}}


def _policy(action: str = "withhold_values", **config: Any) -> SemanticPolicyConfig:
    return SemanticPolicyConfig(
        id="policy.test.rank_only",
        kind="object_access",
        object_ids=[REVENUE, LIFETIME],
        action=action,
        roles=["sales"],
        config=config,
    )


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory):
    return copy_package_config(tmp_path_factory.mktemp("withheld"), "jaffle_shop", preseed_db=True)


def _engine(package, policy: SemanticPolicyConfig) -> Runtime:
    config = replace(load_package_config(str(package)), semantic_policies=[policy])
    return Runtime.from_config(config, source_path=str(package))


@pytest.fixture(scope="module")
def engine(package) -> Iterator[Runtime]:
    runtime = _engine(package, _policy())
    try:
        yield opened(runtime)
    finally:
        runtime.close()


def _select(*items: dict[str, Any]) -> dict[str, Any]:
    return {"select": [*RANK["select"], *items]}


def _value(expression: dict[str, Any], alias: str = "other") -> dict[str, Any]:
    return {"expression": expression, "as": alias}


def _threshold(input_: dict[str, Any]) -> dict[str, Any]:
    predicate = {"kind": "metric_predicate", "entity": "entity.jaffle_customer"}
    predicate |= {"scope_mode": "entity_only", "input": input_, "op": ">=", "value": 100}
    return {"expression": predicate, "op": "=", "value": True}


CONDITIONAL = {
    "kind": "aggregate_if",
    "aggregation": "sum",
    "condition": _threshold({"measure": REVENUE})["expression"],
    "value": {"kind": "column", "entity": "entity.jaffle_order", "column": "order_total_cents"},
}


# Every refused shape, with the reason the guard gives.
REFUSALS = {
    "selected_unordered": ({"order_by": [{"field": STORE, "direction": "ASC"}]}, "not_ranked"),
    "wrapped_aggregation": (
        {"select": [_value({"measure": REVENUE, "aggregation": "max"}, "revenue")]},
        "not_ranked",
    ),
    "ratio_selected": (_select(_value({"metric": AOV})), "value_dependency"),
    "arithmetic_selected": (
        _select(
            _value(
                {
                    "kind": "arithmetic",
                    "op": "multiply",
                    "left": {"measure": REVENUE},
                    "right": {"kind": "literal", "value": 2},
                }
            )
        ),
        "value_dependency",
    ),
    "comparison_selected": (
        _select(
            _value(
                {
                    "kind": "comparison",
                    "op": ">",
                    "left": {"measure": REVENUE},
                    "right": {"kind": "literal", "value": 1000},
                }
            )
        ),
        "value_dependency",
    ),
    "prior_period": (
        {
            **MONTHLY,
            **_select(
                _value(
                    {
                        "kind": "prior_period",
                        "input": {"measure": REVENUE},
                        "offset": {"unit": "month", "value": 1},
                    }
                )
            ),
        },
        "value_dependency",
    ),
    "rolling": (
        {
            **MONTHLY,
            **_select(
                _value(
                    {
                        "kind": "rolling",
                        "input": {"measure": REVENUE},
                        "window": {"unit": "month", "value": 3},
                    }
                )
            ),
        },
        "value_dependency",
    ),
    "cumulative": (
        {**MONTHLY, **_select(_value({"kind": "cumulative", "input": {"measure": REVENUE}}))},
        "value_dependency",
    ),
    "window": (
        {
            **MONTHLY,
            **_select(
                _value({"kind": "period_to_date", "input": {"measure": REVENUE}, "period": "year"})
            ),
        },
        "value_dependency",
    ),
    "metric_filter": (
        {"metric_filters": [{"expression": {"measure": REVENUE}, "op": ">", "value": 1000}]},
        "value_dependency",
    ),
    "threshold": ({"metric_filters": [_threshold({"measure": REVENUE})]}, "value_dependency"),
    "derived_threshold": (
        {"metric_filters": [{"expression": {"metric": AOV}, "op": ">", "value": 10}]},
        "value_dependency",
    ),
    "other_withheld_threshold": (
        {"metric_filters": [_threshold({"measure": LIFETIME})]},
        "value_dependency",
    ),
    # A derived metric reading the withheld measure cannot stand in for it in order_by.
    "derived_ordered": (
        {
            "select": [_value({"metric": AOV}, "aov")],
            "order_by": [{"field": "aov", "direction": "DESC"}],
        },
        "not_ranked",
    ),
    "export": ({"export": True}, "export"),
    "over_max_rank": ({"limit": 11}, "rank_limit"),
    "no_limit": ({"limit": None}, "rank_limit"),
    "ungrouped": ({"group_by": []}, "ungrouped"),
    "opposite_ties": (
        {
            "order_by": [
                {"field": "revenue", "direction": "DESC"},
                {"field": STORE, "direction": "ASC"},
            ]
        },
        "tie_order",
    ),
}


# These shapes are rejected as invalid IR before any policy runs.
INEXPRESSIBLE = {
    "aggregate_if": (_select(_value(CONDITIONAL)), "INVALID_EXPRESSION_AST"),
    "segment_where": (
        {"where": [{"segment": "segment.jaffle.high_value_customers"}]},
        "INVALID_QUERY",
    ),
}


def _codes(engine: Runtime, query: dict[str, Any]) -> list[tuple[str, Any]]:
    """The code and details of validate, compile, execute and MCP execute."""
    report = engine.validate(query)
    outcomes = [(report["errors"][0]["code"], report["errors"][0]["details"])]
    for call in (engine.compile, engine.query):
        with pytest.raises(SemanticLayerError) as exc:
            call(query)
        outcomes.append((exc.value.code, exc.value.details))
    mcp = SemanticLayerMCPAdapter(engine)
    tool = mcp.call_tool("execute", {"query": query, "mode": "run"})
    issue = tool["errors"][0]
    outcomes.append((issue["code"], issue.get("details", {})))
    return outcomes


@pytest.mark.parametrize("name", REFUSALS)
def test_every_other_use_is_refused_on_every_surface(engine, name):
    patch, reason = REFUSALS[name]
    query = {**RANK, **patch}
    for code, details in _codes(engine, query):
        assert code == "POLICY_DENIED"
        assert details["reason"] == f"withheld_{reason}"
        assert REVENUE in details["withheld_objects"] or LIFETIME in details["withheld_objects"]


@pytest.mark.parametrize("name", INEXPRESSIBLE)
@pytest.mark.parametrize("with_policy", [False, True])
def test_invalid_ir_is_refused_independently_of_policy(package, name, with_policy):
    config = replace(
        load_package_config(str(package)), semantic_policies=[_policy()] if with_policy else []
    )
    runtime = Runtime.from_config(config, source_path=str(package))
    patch, expected_code = INEXPRESSIBLE[name]
    try:
        for code, _ in _codes(runtime, {**RANK, **patch}):
            assert code == expected_code
    finally:
        runtime.close()


def test_rank_returns_keys_only_and_flips_with_the_direction(engine):
    bucket = f"{ROLE}__month"
    window = {"time": {**MONTHLY["time"], "start": "2017-01-01", "end": "2017-04-01"}}
    unrestricted = engine.query({**RANK, **window, "limit": None, "policy_context": {}})["rows"]
    every = len(unrestricted)
    assert 3 < every <= 10
    result = engine.query({**RANK, **window})
    assert result["withheld"] == [REVENUE]
    assert all(set(row) == {STORE, bucket} for row in result["rows"])
    assert [row["field"] for row in result["output_columns"]] == [STORE, bucket]
    assert "revenue" not in result["column_types"]
    assert [warning["code"] for warning in result["warnings"]].count("VALUES_WITHHELD") == 1
    # Ties order by every group key in the rank's direction, so ascending is the exact reverse.
    rows = {
        direction: engine.query(
            {
                **RANK,
                **window,
                "limit": every,
                "order_by": [{"field": "revenue", "direction": direction}],
            }
        )["rows"]
        for direction in ("ASC", "DESC")
    }
    assert rows["ASC"] == rows["DESC"][::-1]
    assert result["rows"] == rows["DESC"][:3]
    ranked = sorted(unrestricted, key=lambda row: (row["revenue"], row[STORE], row[bucket]))
    assert rows["ASC"] == [{STORE: row[STORE], bucket: row[bucket]} for row in ranked]
    assert engine.compile({**RANK, **window})["normalized_query"]["order_by"] == [
        {"field": "revenue", "direction": "DESC"},
        {"field": STORE, "direction": "DESC"},
        {"field": bucket, "direction": "DESC"},
    ]


def test_no_response_carries_a_withheld_value(engine):
    values = {row["revenue"] for row in engine.query({**RANK, "policy_context": {}})["rows"]}
    texts = {text for value in values for text in (str(value), f"{float(value):.2f}")}
    assert all(len(text) >= 5 for text in texts)
    full = {**RANK, "verbosity": "full"}
    mcp = SemanticLayerMCPAdapter(engine)
    responses = [
        engine.query(full),
        engine.compile(full),
        engine.validate(full),
        _tool_content(mcp.call_tool("execute", {"query": full, "mode": "run"})),
        _tool_content(mcp.call_tool("execute", {"query": {**full, "limit": 11}, "mode": "run"})),
        engine.validate({**full, **REFUSALS["metric_filter"][0]}),
    ]
    for response in responses:
        serialized = json.dumps(response, default=str)
        assert not [text for text in texts if text in serialized]


def test_valid_values_never_anchors_on_a_withheld_measure(engine):
    result = valid_values_payload(
        engine, dimension_id=STORE, query=RANK, allow_live_query=True, include_counts=True
    )
    assert result["values"]
    assert result["anchor_measure"] not in {REVENUE, LIFETIME}


def test_a_withheld_metric_ranks_and_keeps_its_measures_from_valid_values(package):
    policy = replace(_policy(), object_ids=[CUSTOMERS])
    runtime = _engine(package, policy)
    rank = {**RANK, "select": [_value({"metric": CUSTOMERS}, "revenue")]}
    try:
        result = runtime.query(rank)
        assert result["withheld"] == [CUSTOMERS]
        assert result["rows"] and all(set(row) == {STORE} for row in result["rows"])
        reads = withheld_measure_ids(runtime._config, roles=["sales"])
        assert reads and reads <= {row.id for row in runtime._config.measures}
        values = valid_values_payload(
            runtime, dimension_id=STORE, query=rank, allow_live_query=True, include_counts=True
        )
        assert values["values"] and values["anchor_measure"] not in reads
        # Selected a second time, beside the rank, its values would show.
        twice = {**rank, "select": [*rank["select"], _value({"metric": CUSTOMERS})]}
        for code, details in _codes(runtime, twice):
            assert (code, details["reason"]) == ("POLICY_DENIED", "withheld_value_dependency")
    finally:
        runtime.close()


@pytest.mark.parametrize("action", ["deny"])
@pytest.mark.parametrize("patch", [{}, REFUSALS["metric_filter"][0]], ids=["ordered", "filtered"])
def test_deny_still_refuses_ordering_and_filtering(package, action, patch):
    runtime = _engine(package, _policy(action))
    try:
        for code, details in _codes(runtime, {**RANK, **patch}):
            assert code == "POLICY_DENIED"
            assert REVENUE in details["blocked_objects"]
            assert "withheld_objects" not in details
    finally:
        runtime.close()


def test_max_rank_bounds_the_limit(package):
    runtime = _engine(package, _policy(max_rank=2))
    try:
        assert len(runtime.query({**RANK, "limit": 2})["rows"]) == 2
        for code, details in _codes(runtime, RANK):
            assert (code, details["reason"], details["max_rank"]) == (
                "POLICY_DENIED",
                "withheld_rank_limit",
                2,
            )
    finally:
        runtime.close()


@pytest.mark.parametrize("max_rank", [0, 101, "5", True, 2.5])
def test_invalid_max_rank_is_refused(package, max_rank):
    runtime = _engine(package, _policy(max_rank=max_rank))
    try:
        with pytest.raises(SemanticLayerError, match="max_rank") as exc:
            runtime.compile({**RANK, "policy_context": {}})
        assert exc.value.code == "INVALID_CONFIG"
    finally:
        runtime.close()


def test_segment_on_a_withheld_measure_is_refused(engine):
    segment = "segment.jaffle.high_value_customers"
    report = engine.segment_validate(segment, policy_context=SALES)
    assert report["errors"][0]["details"]["withheld_objects"] == [LIFETIME]
    with pytest.raises(SemanticLayerError) as exc:
        engine.segment_preview(segment, policy_context=SALES)
    assert exc.value.details["reason"] == "withheld_not_ranked"


def test_guard_refuses_without_a_binding(engine):
    """The guard, not its callers, refuses: nothing bound means nothing proved."""
    assert withheld_shape(engine._config, None, {REVENUE: 10}).code == "POLICY_DENIED"
    with pytest.raises(SemanticLayerError) as exc:
        enforce_query_policies(engine._config, iter([REVENUE]), roles=["sales"])
    assert exc.value.details["reason"] == "withheld_unbound"
    # A tie order the runtime would have added is still required of a direct caller.
    with pytest.raises(SemanticLayerError) as exc:
        enforce_query_policies(engine._config, [REVENUE], roles=["sales"], query=RANK)
    assert exc.value.details["reason"] == "withheld_tie_order"


def test_guard_refuses_unresolved_cuts_on_every_surface(engine, monkeypatch):
    query = {**RANK, "order_by": [*RANK["order_by"], {"field": STORE, "direction": "DESC"}]}
    binding = replace(bind_query(engine._config, None, query), unresolved_cuts=("unknown_filter",))
    refusal = withheld_shape(engine._config, binding, {REVENUE: 10})
    assert refusal is not None and refusal.details["reason"] == "withheld_unproven"
    original = engine._bind

    def unresolved(payload, context):
        return replace(original(payload, context), unresolved_cuts=("unknown_filter",))

    monkeypatch.setattr(engine, "_bind", unresolved)
    for code, details in _codes(engine, RANK):
        assert (code, details["reason"]) == ("POLICY_DENIED", "withheld_unproven")


@pytest.mark.parametrize("verbosity", ["minimal", "compact", "full"])
@pytest.mark.parametrize("operation", ["validate", "compile", "query", "mcp"])
def test_granted_rank_retains_withholding_and_redacted_descriptors(package, verbosity, operation):
    runtime = _engine(package, replace(_policy(), object_ids=[AOV]))
    query = {
        **RANK,
        "select": [_value({"metric": AOV}, "revenue")],
        "verbosity": verbosity,
        "policy_context": {**SALES, "metric_allowlist": [AOV], "dimension_allowlist": [STORE]},
    }
    try:
        values = runtime.query({**query, "policy_context": {}})["rows"]
        if operation == "mcp":
            result = SemanticLayerMCPAdapter(runtime).call_tool(
                "execute", {"query": query, "mode": "run"}
            )
        else:
            result = getattr(runtime, operation)(query)
        assert result["ok"]
        assert result["withheld"] == [AOV]
        assert [column["field"] for column in result["output_columns"]] == [STORE]
        assert "revenue" not in result.get("column_types", {})
        assert all("revenue" not in row for row in result.get("rows", []))
        warning = next(row for row in result["warnings"] if row["code"] == "VALUES_WITHHELD")
        assert warning["object_ids"] == [AOV]
        serialized = json.dumps(result, default=str)
        assert REVENUE not in serialized
        assert not any(str(row["revenue"]) in serialized for row in values)
    finally:
        runtime.close()
