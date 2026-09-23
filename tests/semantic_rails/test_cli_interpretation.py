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

from semantic_rails import dev_cli
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
            "Revenue / AOV over a rolling one period, cumulative AOV and year-to-date Revenue, "
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
            "conversion from Orders to AOV within 7 days, where AOV > 10 and "
            "metric predicate [with entity]",
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
    assert dev_cli.describe_query(query, LABELS) == expected


def test_unknown_ids_are_shown_as_ids() -> None:
    query = {"select": [{"expression": {"measure": "measure.unknown"}}], "group_by": ["d.x"]}

    assert dev_cli.describe_query(query) == "measure.unknown by d.x"


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

    monkeypatch.setattr(dev_cli, "_runtime_from_ref", lambda _ref: _StubRuntime())
    monkeypatch.setattr(dev_cli, "plan_payload", lambda *_a, **_k: plan)
    monkeypatch.setattr(dev_cli, "resolve_catalog", broken_catalog)

    report = dev_cli.ask_report(PackageReference(source_path="/nowhere"), question="orders")

    assert report["interpretation"] == "Orders"


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

    expected = "Revenue (sum) by Store name, per month of Order time"
    assert f"\nInterpreted as: {expected}\n" in run()
    assert json.loads(run("--json"))["interpretation"] == expected
