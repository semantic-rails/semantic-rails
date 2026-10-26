"""`ask` restates what the executed query computes.

The "Interpreted as" line is built from the planned query call, never from the
question, so a misread question is visible instead of silent. Anything the
restatement can't spell out is listed as ``[with ...]`` rather than dropped.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import semantic_rails.cli.interpretation as interpretation
import semantic_rails.cli.reports as reports
from semantic_rails.config_validation import PackageReference
from semantic_rails.contracts import load_contract
from semantic_rails.errors import SemanticLayerError

REPO_ROOT = Path(__file__).resolve().parents[2]
LABELS = {
    "measure.revenue": "Revenue",
    "measure.orders": "Orders",
    "metric.aov": "AOV",
    "dimension.store": "Store",
    "entity.customer": "Customer",
    "temporal_role.ordered_at": "Order time",
}


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            {
                "select": [
                    {
                        "expression": {
                            "kind": "measure",
                            "measure": "measure.orders",
                            "aggregation": "count_distinct",
                        }
                    }
                ],
                "group_by": ["dimension.store"],
                "where": [{"field": "dimension.store", "op": "is", "value": "New Orleans"}],
            },
            'Orders (count distinct) by Store, where Store is "New Orleans"',
        ),
        (
            {
                "select": [{"expression": "metric.aov", "as": "aov"}],
                "time": {"temporal_role": "temporal_role.ordered_at", "grain": "month"},
                "order_by": [{"field": "aov", "direction": "desc"}],
                "limit": 5,
            },
            "AOV, per month of Order time, first 5 rows by AOV descending",
        ),
        ({"select": ["measure.revenue"], "limit": 3}, "Revenue, first 3 rows"),
        (
            {
                "select": [
                    "measure.revenue",
                    {
                        "expression": {
                            "kind": "prior_period",
                            "input": "measure.revenue",
                            "offset": {"unit": "year", "value": 1},
                        }
                    },
                ],
                "time": {"last": {"unit": "day", "value": 30}},
            },
            "Revenue and Revenue 1 year earlier, in the last 30 days",
        ),
        (
            {
                "select": [
                    {
                        "expression": {
                            "kind": "prior_period",
                            "input": {"kind": "measure", "measure": "measure.revenue"},
                            "offset": {"unit": "month", "value": 3},
                        }
                    }
                ],
                "time": {"start": "2024-01-01", "end": "2025-01-01"},
            },
            "Revenue 3 months earlier, from 2024-01-01 to before 2025-01-01",
        ),
        (
            {"select": ["metric.aov"], "time": {"this": "month", "grain": "day"}},
            "AOV, per day, this month so far",
        ),
        (
            {
                "select": [
                    {
                        "expression": {
                            "kind": "ratio",
                            "numerator": "measure.revenue",
                            "denominator": {"kind": "rolling", "input": "metric.aov"},
                        }
                    },
                    {"expression": {"kind": "cumulative", "input": "metric.aov"}},
                    {
                        "expression": {
                            "kind": "period_to_date",
                            "period": "year",
                            "input": "measure.revenue",
                        }
                    },
                ],
                "time": {"temporal_role": "temporal_role.ordered_at", "start": "2024-01-01"},
            },
            "Revenue / (AOV over a rolling one period), cumulative AOV and year-to-date Revenue, "
            "on Order time, from 2024-01-01",
        ),
        (
            {
                "select": [
                    {
                        "expression": {
                            "kind": "scoped_aggregate",
                            "measure": "measure.revenue",
                            "aggregation": "sum",
                            "temporal_role": "temporal_role.ordered_at",
                            "where": [
                                {"field": "dimension.store", "op": "is", "value": ["A", "B"]},
                                {"field": "dimension.store", "op": "missing", "value": True},
                            ],
                            "anchor": {"kind": "entity"},
                        }
                    }
                ],
                "time": {"end": "2025-01-01"},
            },
            'Revenue (sum) on Order time where Store is ("A", "B") and Store has no value '
            "[with anchor], before 2025-01-01",
        ),
        (
            {
                "select": [
                    {
                        "expression": {
                            "kind": "conversion",
                            "base": "measure.orders",
                            "converted": "metric.aov",
                            "entity": "entity.customer",
                            "window": {"unit": "day", "value": 7},
                            "matching_mode": "first_converted_after_base",
                        }
                    }
                ],
                "where": [
                    {
                        "entity": "entity.customer",
                        "having": [{"field": "measure.orders", "op": "gte", "value": 5}],
                        "over": "all_time",
                    }
                ],
                "having": [{"field": "metric.aov", "op": "gt", "value": 10}],
            },
            "conversion from Orders to AOV per Customer within 7 days, matching the first "
            "conversion after each base event, where the Customer's Orders >= 5 over all time "
            "and AOV > 10",
        ),
        (
            {
                "select": ["measure.orders"],
                "where": [
                    {
                        "entity": "entity.customer",
                        "any": [{"field": "dimension.store", "op": "not", "value": "A"}],
                    },
                    {
                        "entity": "entity.customer",
                        "none": [{"field": "dimension.store", "op": "contains", "value": "x"}],
                    },
                ],
            },
            'Orders, where some Customer has Store is not "A" and no Customer has Store '
            'contains "x"',
        ),
        (
            {
                "select": [
                    {
                        "expression": {
                            "kind": "aggregate_if",
                            "aggregation": "count",
                            "condition": {"kind": "comparison"},
                        }
                    }
                ],
                "routes": [{"from": "entity.a", "to": "entity.b", "via": ["relationship.x"]}],
            },
            "aggregate if [with aggregation, condition] [with routes]",
        ),
        ({}, "no measures"),
    ],
)
def test_describe_query_restates_what_runs(query: dict[str, Any], expected: str) -> None:
    assert interpretation.describe_query(query, LABELS) == expected


def _ratio(numerator: Any, denominator: Any) -> dict[str, Any]:
    return {"kind": "ratio", "numerator": numerator, "denominator": denominator}


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (
            _ratio("measure.revenue", _ratio("metric.aov", "measure.orders")),
            "Revenue / (AOV / Orders)",
        ),
        (
            _ratio(_ratio("measure.revenue", "metric.aov"), "measure.orders"),
            "(Revenue / AOV) / Orders",
        ),
        (
            {
                "kind": "arithmetic",
                "op": "divide",
                "left": _ratio("measure.revenue", "measure.orders"),
                "right": {"kind": "literal", "value": 2},
            },
            "((Revenue / Orders) / 2)",
        ),
        (
            {
                "kind": "arithmetic",
                "op": "subtract",
                "left": "measure.revenue",
                "right": "metric.aov",
            },
            "(Revenue - AOV)",
        ),
        (
            {"kind": "cumulative", "input": _ratio("measure.revenue", "measure.orders")},
            "cumulative (Revenue / Orders)",
        ),
    ],
)
def test_compound_operands_keep_their_grouping(expression: dict[str, Any], expected: str) -> None:
    query = {"select": [{"expression": expression}]}
    assert interpretation.describe_query(query, LABELS) == expected


def _conversion(**changes: Any) -> dict[str, Any]:
    expression = {
        "kind": "conversion",
        "base": "measure.orders",
        "converted": "metric.aov",
        "entity": "entity.customer",
        "window": {"unit": "day", "value": 7},
        "matching_mode": "first_converted_after_base",
    }
    return {"select": [{"expression": {**expression, **changes}}]}


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (_conversion(), _conversion(entity="entity.household")),
        (_conversion(), _conversion(matching_mode="closest_converted_after_base")),
        (
            _conversion(matching_mode=None, matching="first_converted_after_base"),
            _conversion(matching_mode=None),
        ),
        ({"time": {"grain": "month"}}, {"time": {"grain": "month", "fill": True}}),
        (
            {"where": [{"entity": "entity.customer", "having": [], "over": "window"}]},
            {"where": [{"entity": "entity.customer", "having": [], "over": "all_time"}]},
        ),
    ],
)
def test_a_change_in_what_runs_changes_the_restatement(
    first: dict[str, Any], second: dict[str, Any]
) -> None:
    assert interpretation.describe_query(first, LABELS) != interpretation.describe_query(
        second, LABELS
    )


def test_fill_is_spelled_out() -> None:
    query = {"select": ["measure.revenue"], "time": {"grain": "month", "fill": True}}
    assert interpretation.describe_query(query, LABELS) == (
        "Revenue, per month, including periods with no data"
    )


_SCHEMA = load_contract("query.v2.json")
_CONDITION = {"field": "dimension.store", "op": "is", "value": "B"}


def _select_expression(query: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    return {"select": [{"expression": {**query["select"][0]["expression"], key: value}}]}


_BASES: dict[str, tuple[dict[str, Any], Any]] = {
    # schema block -> (a call, a function placing a block property into that call)
    "query": (
        {"select": [{"expression": "metric.aov", "as": "aov"}], "limit": 5},
        lambda query, key, value: {**query, key: value},
    ),
    "time": (
        {"time": {"temporal_role": "temporal_role.ordered_at"}},
        lambda query, key, value: {"time": {**query["time"], key: value}},
    ),
    "Condition": (
        {"where": [{"field": "dimension.store", "value": "A"}]},
        lambda query, key, value: {"where": [{**query["where"][0], key: value}]},
    ),
    "EntityFilter": (
        {"where": [{"entity": "entity.customer"}]},
        lambda query, key, value: {"where": [{**query["where"][0], key: value}]},
    ),
    **{
        block: (
            {"select": [{"expression": {"kind": block.removeprefix("Kind_")}}]},
            _select_expression,
        )
        for block in _SCHEMA["$defs"]
        if block.startswith("Kind_")
    },
}


def _properties(block: str) -> dict[str, Any]:
    if block == "query":
        return dict(_SCHEMA["properties"])
    if block == "time":
        return dict(_SCHEMA["properties"]["time"]["properties"])
    return dict(_SCHEMA["$defs"][block]["properties"])


def _sample(key: str, spec: dict[str, Any]) -> Any:
    samples = {
        "op": "not",
        "direction": "desc",
        "fill": True,
        "value": "B",
        "select": ["measure.orders"],
        "group_by": ["dimension.store"],
        "where": [_CONDITION],
        "having": [_CONDITION],
        "any": [_CONDITION],
        "none": [_CONDITION],
        "order_by": [{"field": "aov", "direction": "desc"}],
        "routes": [{"from": "entity.a", "to": "entity.b", "via": ["relationship.x"]}],
        "last": {"unit": "day", "value": 3},
    }
    if key in samples:
        return samples[key]
    if "$ref" in spec:
        return "measure.orders"
    if "enum" in spec:
        return next(value for value in spec["enum"] if value)
    kind = spec.get("type", "object")
    kind = kind[0] if isinstance(kind, list) else kind
    return {
        "string": "sample",
        "integer": 3,
        "number": 3,
        "boolean": True,
        "array": ["sample"],
    }.get(kind, {"sample": 1})


@pytest.mark.parametrize(
    ("block", "key"),
    [
        (block, key)
        for block in _BASES
        for key in _properties(block)
        # The engine ignores version, and an alias only names the output column.
        if key not in {"kind", "version"}
    ],
)
def test_every_call_key_is_spelled_out_or_flagged(block: str, key: str) -> None:
    query, place = _BASES[block]
    before = interpretation.describe_query(query, LABELS)
    after = interpretation.describe_query(
        place(query, key, _sample(key, _properties(block)[key])), LABELS
    )
    assert after != before, f"{block}.{key} changed what runs but not the restatement"


def test_unknown_ids_are_shown_as_ids() -> None:
    query = {"select": ["measure.unknown"], "group_by": ["d.x"]}

    assert interpretation.describe_query(query) == "measure.unknown by d.x"


class _StubRuntime:
    package_id = "stub"
    warehouse = "duckdb"

    def close(self) -> None:
        pass


def test_labels_fall_back_to_the_plan_when_the_catalog_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = {
        "ok": True,
        "best": {
            "query": {"select": ["measure.orders"]},
            "resolved": [{"id": "measure.orders", "label": "Orders"}],
        },
    }

    def broken_catalog(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise SemanticLayerError("INVALID_CONFIG", "catalog unavailable")

    monkeypatch.setattr(reports, "_runtime_from_ref", lambda _ref: _StubRuntime())
    monkeypatch.setattr(reports, "plan_payload", lambda *_a, **_k: plan)
    monkeypatch.setattr(interpretation, "resolve_catalog", broken_catalog)

    report = reports.ask_report(PackageReference(source_path="/nowhere"), question="orders")

    assert report["interpretation"] == "Orders"


def test_ask_report_restates_ordering_before_the_default_execution_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OrderedRuntime(_StubRuntime):
        def query(self, query: dict[str, Any], **options: Any) -> dict[str, Any]:
            assert query["limit"] == 21
            assert options["limits"]["max_rows"] == 20
            descending = query["order_by"][0]["direction"] == "desc"
            values = sorted(range(30), reverse=descending)[:20]
            return {"ok": True, "rows": [{"revenue": value} for value in values]}

    direction = "asc"

    def planned(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "best": {
                "query": {
                    "select": ["measure.revenue"],
                    "order_by": [{"field": "revenue", "direction": direction}],
                }
            },
        }

    monkeypatch.setattr(reports, "_runtime_from_ref", lambda _ref: OrderedRuntime())
    monkeypatch.setattr(reports, "plan_payload", planned)
    monkeypatch.setattr(reports, "_object_labels", lambda *_args: LABELS)

    ascending = reports.ask_report(
        PackageReference(source_path="/nowhere"), question="revenue", execute=True
    )
    direction = "desc"
    descending = reports.ask_report(
        PackageReference(source_path="/nowhere"), question="revenue", execute=True
    )

    assert ascending["query"].get("limit") is None
    assert ascending["interpretation"] == "Revenue, ordered by Revenue ascending"
    assert descending["interpretation"] == "Revenue, ordered by Revenue descending"
    assert ascending["result"]["rows"][0] == {"revenue": 0}
    assert descending["result"]["rows"][0] == {"revenue": 29}


def test_ask_prints_what_the_query_computes(tmp_path: Path) -> None:
    env = dict(
        os.environ,
        SEMANTIC_RAILS_HOME=str(tmp_path / "home"),
        PYTHONPATH=os.pathsep.join(filter(None, [str(REPO_ROOT), os.getenv("PYTHONPATH")])),
    )
    args = ("ask", "--package", "jaffle_shop", "monthly revenue by store")

    def run(*extra: str) -> str:
        proc = subprocess.run(
            [sys.executable, "-m", "semantic_rails", *args, *extra],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    # "store" names the Store entity: its key, with its name beside it. The plan selects the
    # measure by its id, at its own statistic.
    expected = (
        "Revenue by Store Id, Store name, per month of Order time, "
        "ordered by period ascending, Store Id ascending, Store name ascending"
    )
    assert f"\nInterpreted as: {expected}\n" in run()
    assert json.loads(run("--json"))["interpretation"] == expected
