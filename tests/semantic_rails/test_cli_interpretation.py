"""`ask` restates what the executed query computes.

The "Interpreted as" line is built from the planned Query IR, never from the
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
                    {"expression": {"measure": "measure.orders", "aggregation": "count_distinct"}}
                ],
                "group_by": ["dimension.store"],
                "where": [{"field": "dimension.store", "op": "=", "value": "New Orleans"}],
            },
            'Orders (count distinct) by Store, where Store = "New Orleans"',
        ),
        (
            {
                "select": [{"expression": {"metric": "metric.aov"}, "as": "aov"}],
                "time": {"temporal_role": "temporal_role.ordered_at", "grain": "month"},
                "order_by": [{"field": "aov", "direction": "DESC"}],
                "limit": 5,
            },
            "AOV, per month of Order time, first 5 rows by AOV descending",
        ),
        (
            {
                "select": [{"expression": {"measure": "measure.revenue"}}],
                "order_by": [{"field": "measure.revenue", "direction": "ASC"}],
            },
            "Revenue, ordered by Revenue ascending",
        ),
        (
            {
                "select": [{"expression": {"measure": "measure.revenue"}}],
                "order_by": [{"field": "measure.revenue", "direction": "DESC"}],
            },
            "Revenue, ordered by Revenue descending",
        ),
        (
            {"select": [{"expression": {"measure": "measure.revenue"}}], "limit": 0},
            "Revenue, first 0 rows",
        ),
        (
            {
                "select": [{"expression": {"measure": "measure.revenue"}}],
                "order_by": [{"field": "measure.revenue", "direction": "DESC"}],
                "limit": 0,
            },
            "Revenue, first 0 rows by Revenue descending",
        ),
        (
            {
                "select": [
                    {"expression": {"measure": "measure.revenue"}},
                    {
                        "expression": {
                            "kind": "prior_period",
                            "measure": "measure.revenue",
                            "offset": -1,
                            "grain": "year",
                        }
                    },
                ],
                "time": {"range": {"last": {"unit": "day", "value": 30}}},
            },
            "Revenue and Revenue 1 year earlier, in the last 30 days",
        ),
        (
            {
                "select": [
                    {
                        "expression": {
                            "kind": "prior_period",
                            "input": {"kind": "aggregate", "measure": "measure.revenue"},
                            "offset": {"unit": "month", "value": 3},
                        }
                    }
                ],
                "time": {"start": "2024-01-01", "end": "2025-01-01"},
            },
            "Revenue 3 months earlier, from 2024-01-01 to before 2025-01-01",
        ),
        (
            {
                "select": [
                    {
                        "expression": {
                            "kind": "ratio",
                            "numerator": {"measure": "measure.revenue"},
                            "denominator": {"kind": "rolling", "input": {"metric": "metric.aov"}},
                        }
                    },
                    {"expression": {"kind": "cumulative", "input": {"metric": "metric.aov"}}},
                    {
                        "expression": {
                            "kind": "period_to_date",
                            "period": "year",
                            "input": {"measure": "measure.revenue"},
                        }
                    },
                ],
                "time": {"temporal_role": "temporal_role.ordered_at", "start": "2024-01-01"},
            },
            "Revenue / (AOV over a rolling one period), cumulative AOV and year-to-date Revenue, "
            "per Order time value, from 2024-01-01",
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
                                {"field": "dimension.store", "op": "IN", "value": ["A", "B"]},
                                {"field": "dimension.store", "op": "IS NULL"},
                            ],
                            "anchor": {"kind": "entity"},
                        }
                    }
                ],
                "time": {"end": "2025-01-01"},
            },
            'Revenue (sum) on Order time where Store IN ("A", "B") and Store is null '
            "[with anchor], before 2025-01-01",
        ),
        (
            {
                "select": [
                    {
                        "expression": {
                            "kind": "conversion",
                            "base": {"measure": "measure.orders"},
                            "converted": {"metric": "metric.aov"},
                            "entity": "entity.customer",
                            "window": {"unit": "day", "value": 7},
                            "matching_mode": "first_converted_after_base",
                        }
                    }
                ],
                "metric_filters": [
                    {"expression": {"metric": "metric.aov"}, "op": ">", "value": 10},
                    {"expression": {"kind": "metric_predicate", "entity": "entity.customer"}},
                ],
            },
            "conversion from Orders to AOV per Customer within 7 days, matching the first "
            "conversion after each base event, where AOV > 10 and metric predicate [with entity]",
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
                "temporal_role_overrides": {"measure.orders": "temporal_role.ordered_at"},
            },
            "aggregate if [with aggregation, condition] [with temporal_role_overrides]",
        ),
        ({}, "no measures"),
    ],
)
def test_describe_query_restates_what_runs(query: dict[str, Any], expected: str) -> None:
    assert interpretation.describe_query(query, LABELS) == expected


def _ratio(numerator: dict[str, Any], denominator: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "ratio", "numerator": numerator, "denominator": denominator}


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (
            _ratio(
                {"measure": "measure.revenue"},
                _ratio({"metric": "metric.aov"}, {"measure": "measure.orders"}),
            ),
            "Revenue / (AOV / Orders)",
        ),
        (
            _ratio(
                _ratio({"measure": "measure.revenue"}, {"metric": "metric.aov"}),
                {"measure": "measure.orders"},
            ),
            "(Revenue / AOV) / Orders",
        ),
        (
            {
                "kind": "arithmetic",
                "op": "divide",
                "left": _ratio({"measure": "measure.revenue"}, {"measure": "measure.orders"}),
                "right": {"kind": "literal", "value": 2},
            },
            "((Revenue / Orders) / 2)",
        ),
        (
            {
                "kind": "binary",
                "op": "subtract",
                "left": {"kind": "measure_ref", "measure": "measure.revenue"},
                "right": {"kind": "metric", "metric": "metric.aov"},
            },
            "(Revenue - AOV)",
        ),
        (
            {
                "kind": "cumulative",
                "input": _ratio({"measure": "measure.revenue"}, {"measure": "measure.orders"}),
            },
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
        "base": {"measure": "measure.orders"},
        "converted": {"metric": "metric.aov"},
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
        ({"time": {"grain": "month"}}, {"time": {"grain": "month", "calendar_id": "fiscal"}}),
    ],
)
def test_a_change_in_what_runs_changes_the_restatement(
    first: dict[str, Any], second: dict[str, Any]
) -> None:
    assert interpretation.describe_query(first, LABELS) != interpretation.describe_query(
        second, LABELS
    )


def test_fill_and_calendar_are_spelled_out() -> None:
    query = {
        "select": [{"expression": {"measure": "measure.revenue"}}],
        "time": {"grain": "month", "fill": True, "calendar_id": "fiscal"},
    }
    assert interpretation.describe_query(query, LABELS) == (
        "Revenue, per month, on calendar fiscal, including periods with no data"
    )


_SCHEMA = json.loads((REPO_ROOT / "schemas" / "query_ir.v1.json").read_text())
_BASES: dict[str, tuple[dict[str, Any], Any]] = {
    # schema block -> (a query, a function placing a block property into that query)
    "query": (
        {"select": [{"expression": {"metric": "metric.aov"}, "as": "aov"}], "limit": 5},
        lambda query, key, value: {**query, key: value},
    ),
    "SelectItem": (
        {"select": [{"expression": {"metric": "metric.aov"}}]},
        lambda query, key, value: {"select": [{**query["select"][0], key: value}]},
    ),
    "TimeBlock": (
        {"time": {"temporal_role": "temporal_role.ordered_at"}},
        lambda query, key, value: {"time": {**query["time"], key: value}},
    ),
    "WhereFilter": (
        {"where": [{"field": "dimension.store", "value": "A"}]},
        lambda query, key, value: {"where": [{**query["where"][0], key: value}]},
    ),
    "MetricFilter": (
        {"metric_filters": [{"expression": {"metric": "metric.aov"}}]},
        lambda query, key, value: {"metric_filters": [{**query["metric_filters"][0], key: value}]},
    ),
    "ConversionExpr": (
        {
            "select": [
                {
                    "expression": {
                        "kind": "conversion",
                        "base": {"measure": "measure.orders"},
                        "converted": {"metric": "metric.aov"},
                    }
                }
            ]
        },
        lambda query, key, value: {
            "select": [{"expression": {**query["select"][0]["expression"], key: value}}]
        },
    ),
}


def _sample(key: str, spec: dict[str, Any]) -> Any:
    samples = {"calendar_id": "fiscal", "fill": True, "op": "!=", "direction": "DESC"}
    if key in samples:
        return samples[key]
    if "enum" in spec:
        return next(value for value in spec["enum"] if value)
    kind = spec.get("type", "object")
    kind = kind[0] if isinstance(kind, list) else kind
    return {
        "string": "sample",
        "integer": 3,
        "number": 3,
        "boolean": True,
        "array": [{"field": "aov", "direction": "DESC"}] if key == "order_by" else ["sample"],
    }.get(kind, {"sample": 1})


def _expression_kinds() -> dict[str, str]:
    """Every expression block in the schema that has a ``kind``, with one of its kinds."""

    kinds = {}
    for name, block in _SCHEMA["$defs"].items():
        spec = block.get("properties", {}).get("kind", {})
        values = [spec["const"]] if "const" in spec else list(spec.get("enum", []))
        if values:
            kinds[name] = values[0]
    return kinds


for _block, _kind in _expression_kinds().items():
    _BASES.setdefault(
        _block,
        (
            {"select": [{"expression": {"kind": _kind}}]},
            lambda query, key, value: {
                "select": [{"expression": {**query["select"][0]["expression"], key: value}}]
            },
        ),
    )

# An alias only names the output column; it doesn't change what runs.
_NAMES_ONLY = {("SelectItem", "as")}


@pytest.mark.parametrize(
    ("block", "key"),
    [
        (block, key)
        for block in _BASES
        for key in (_SCHEMA if block == "query" else _SCHEMA["$defs"][block])["properties"]
        if key != "kind"
        and (block, key) not in _NAMES_ONLY
        and not (block == "query" and key in interpretation._RESPONSE_QUERY_KEYS)
    ],
)
def test_every_query_ir_key_is_spelled_out_or_flagged(block: str, key: str) -> None:
    query, place = _BASES[block]
    spec = (_SCHEMA if block == "query" else _SCHEMA["$defs"][block])["properties"][key]
    before = interpretation.describe_query(query, LABELS)
    after = interpretation.describe_query(place(query, key, _sample(key, spec)), LABELS)
    assert after != before, f"{block}.{key} changed what runs but not the restatement"


def test_unknown_ids_are_shown_as_ids() -> None:
    query = {"select": [{"expression": {"measure": "measure.unknown"}}], "group_by": ["d.x"]}

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
            "query_ir": {"select": [{"expression": {"measure": "measure.orders"}}]},
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
        def query(self, query: dict[str, Any]) -> dict[str, Any]:
            assert query["limit"] == 21
            assert query["limits"]["max_rows"] == 20
            descending = query["order_by"][0]["direction"] == "DESC"
            values = sorted(range(30), reverse=descending)[:20]
            return {"ok": True, "rows": [{"revenue": value} for value in values]}

    direction = "ASC"

    def planned(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "best": {
                "query_ir": {
                    "select": [{"expression": {"measure": "measure.revenue"}}],
                    "order_by": [{"field": "measure.revenue", "direction": direction}],
                }
            },
        }

    monkeypatch.setattr(reports, "_runtime_from_ref", lambda _ref: OrderedRuntime())
    monkeypatch.setattr(reports, "plan_payload", planned)
    monkeypatch.setattr(reports, "_object_labels", lambda *_args: LABELS)

    ascending = reports.ask_report(
        PackageReference(source_path="/nowhere"), question="revenue", execute=True
    )
    direction = "DESC"
    descending = reports.ask_report(
        PackageReference(source_path="/nowhere"), question="revenue", execute=True
    )

    assert ascending["query"].get("limit") is None
    assert ascending["interpretation"] == "Revenue, ordered by Revenue ascending"
    assert descending["interpretation"] == "Revenue, ordered by Revenue descending"
    assert ascending["result"]["rows"][0] == {"revenue": 0}
    assert descending["result"]["rows"][0] == {"revenue": 29}


def test_ask_prints_what_the_query_computes(tmp_path: Path) -> None:
    env = dict(os.environ, SEMANTIC_RAILS_HOME=str(tmp_path / "home"), PYTHONPATH=str(REPO_ROOT))
    args = ("ask", "--package", "jaffle_shop", "monthly revenue by store")

    def run(*extra: str) -> str:
        proc = subprocess.run(
            [sys.executable, "-m", "semantic_rails", *args, *extra],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    expected = (
        "Revenue (sum) by Store name, per month of Order time, "
        "ordered by time ascending, Store name ascending"
    )
    assert f"\nInterpreted as: {expected}\n" in run()
    assert json.loads(run("--json"))["interpretation"] == expected
