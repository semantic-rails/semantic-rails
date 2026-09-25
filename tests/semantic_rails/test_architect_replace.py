"""replace on upsert_model, upsert_metric and upsert_segment: rewrite, report, keep identity."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_service import ArchitectProject, _replaced
from semantic_rails.architect_transactions import project_revision
from semantic_rails.errors import SemanticLayerError
from tests.semantic_rails.dbt_warehouse import write_orders_package

REPO = Path(__file__).resolve().parents[2]

# upsert_model arguments that rewrite the orders model without its status dimension.
ORDERS: dict[str, Any] = {
    "model_id": "orders",
    "entity_key": "order",
    "relation": "main_marts.fct_orders",
    "primary_key": ["order_id"],
    "times": {
        "ordered_at": {
            "label": "Order time",
            "column": "ordered_at",
            "kind": "timestamp",
            "class": "event_time",
            "default": True,
        }
    },
    "measures": {
        "order_count": {
            "label": "Order Count",
            "kind": "entity_count",
            "entity_key": "order_id",
            "value_type": "count",
        },
        "order_total": {
            "label": "Order Total",
            "kind": "aggregate",
            "expr": "order_total",
            "default_agg": "sum",
            "value_type": "currency",
        },
    },
}

SEGMENT: dict[str, Any] = {
    "id": "segment.shop.large_orders",  # not the id derived from its key
    "label": "Big orders",
    "entity": "entity.shop_order",
    "basis_metric": "metric.shop.gross_revenue",
    "preview_dimensions": ["dimension.shop_order_status"],
    "membership": {"where": [{"field": "dimension.shop_order_status", "op": "=", "value": "big"}]},
}


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    shop = write_orders_package(tmp_path, seed={"kind": "external"})
    metrics = yaml.safe_load((shop / "metrics" / "core.yml").read_text(encoding="utf-8"))
    metrics["metrics"]["revenue"]["as"] = "metric.shop.gross_revenue"
    (shop / "metrics" / "core.yml").write_text(yaml.safe_dump(metrics, sort_keys=False))
    (shop / "segments").mkdir()
    (shop / "segments" / "core.yml").write_text(
        yaml.safe_dump({"segments": {"big_orders": SEGMENT}}, sort_keys=False)
    )
    return tmp_path


def _yaml(workspace: Path, relative: str) -> dict[str, Any]:
    return dict(yaml.safe_load((workspace / "shop" / relative).read_text(encoding="utf-8")))


STATUS = {"status": {"label": "Status", "kind": "categorical"}}


@pytest.mark.parametrize(
    ("arguments", "dropped"),
    [
        ({"dimensions": STATUS}, ["label"]),
        ({"dimensions": STATUS, "label": "Orders"}, []),
        ({"dimensions": STATUS, "label": "Orders", "description": "Orders."}, []),
        # The segment filters on status, so the parse gate rolls this one back.
        ({}, ["dimensions.status", "label"]),
    ],
)
@pytest.mark.parametrize("batch", [False, True])
def test_model_replace_rewrites_it_from_the_arguments(
    workspace: Path, arguments: dict[str, Any], dropped: list[str], batch: bool
) -> None:
    project = ArchitectProject(workspace / "shop", workspace_root=workspace)
    before = project_revision(workspace / "shop")
    call = {**ORDERS, **arguments, "replace": True}

    if batch:
        report = project.upsert_models([call]).report
        assert report["models"][0]["dropped_fields"] == dropped
    else:
        report = project.upsert_model(**call).report
        assert report["dropped_fields"] == dropped

    if "dimensions" not in arguments:
        assert report["status"] == "rolled_back_after_parse_error"
        assert project_revision(workspace / "shop") == before
        return
    assert report["ok"] is True, report
    assert _yaml(workspace, "models/orders.yml")["model"] == {
        "id": "orders",
        "relation": ORDERS["relation"],
        "entities": {"order": {}, "customer": {}},
        "description": "One row per Order.",  # the default, as on create
        **{field: ORDERS[field] for field in ("times", "measures")},
        **arguments,
    }


def test_a_replaced_description_is_reported(workspace: Path) -> None:
    project = ArchitectProject(workspace / "shop", workspace_root=workspace)
    project.upsert_model(**ORDERS, description="Every order, one row each.")

    report = project.upsert_model(**ORDERS, dimensions=STATUS, label="Orders", replace=True).report

    assert report["dropped_fields"] == ["description"]


@pytest.mark.parametrize(
    ("current", "spec", "expected"),
    [
        ({"as": "metric.a", "label": "A"}, {"label": "B"}, {"label": "B", "as": "metric.a"}),
        (
            {"id": "x", "name": "n", "kind": "k"},
            {"kind": "j"},
            {"kind": "j", "id": "x", "name": "n"},
        ),
        ({"as": "metric.a"}, {"as": "metric.b"}, {"as": "metric.b"}),  # a restated id wins
    ],
)
def test_replace_keeps_identity_it_does_not_restate(
    current: dict[str, Any], spec: dict[str, Any], expected: dict[str, Any]
) -> None:
    assert _replaced(current, spec) == expected


def test_a_merge_reports_nothing_dropped(workspace: Path) -> None:
    report = ArchitectProject(workspace / "shop", workspace_root=workspace).upsert_model(**ORDERS)

    assert report.report["ok"] is True
    assert "dropped_fields" not in report.report
    assert "status" in _yaml(workspace, "models/orders.yml")["model"]["dimensions"]


def test_fact_models_are_refused(tmp_path: Path) -> None:
    shutil.copytree(REPO / "configs" / "semantic_rails" / "jaffle_shop", tmp_path / "jaffle")
    project = ArchitectProject(tmp_path / "jaffle", workspace_root=tmp_path)
    before = project_revision(tmp_path / "jaffle")

    for replace in (False, True):
        with pytest.raises(SemanticLayerError, match="fact model"):
            project.upsert_model(
                model_id="daily_metrics",
                entity_key="daily",
                relation="jaffle_daily_metric_rollup",
                primary_key=["date_day"],
                replace=replace,
            )
    assert project_revision(tmp_path / "jaffle") == before


def test_mcp_replace_keeps_public_ids(workspace: Path) -> None:
    server = create_architect_mcp_server(workspace_root=workspace)

    async def run() -> list[dict[str, Any]]:
        async with create_connected_server_and_client_session(server) as session:

            async def call(name: str, **values: Any) -> dict[str, Any]:
                revision = project_revision(workspace / "shop")
                result = await session.call_tool(
                    name,
                    {
                        "project_path": "shop",
                        "expected_revision": revision,
                        "idempotency_key": name,
                        "replace": True,
                        **values,
                    },
                )
                return dict(result.structuredContent or {})

            return [
                await call("upsert_model", **ORDERS, dimensions=STATUS, label="Order facts"),
                await call(
                    "upsert_metric",
                    metric_key="revenue",
                    spec={
                        "label": "Revenue",
                        "kind": "aggregate",
                        "measure": "order_total",
                        "value_type": "currency",
                    },
                ),
                await call(
                    "upsert_segment",
                    segment_key="big_orders",
                    spec={
                        key: value
                        for key, value in SEGMENT.items()
                        if key not in {"id", "preview_dimensions"}
                    },
                ),
            ]

    model, metric, segment = asyncio.run(run())

    for report in (model, metric, segment):
        assert report["ok"] is True, report
    assert model["dropped_fields"] == []
    assert _yaml(workspace, "models/orders.yml")["model"]["label"] == "Order facts"
    revenue = _yaml(workspace, "metrics/core.yml")["metrics"]["revenue"]
    assert revenue["as"] == "metric.shop.gross_revenue" and "description" not in revenue
    big_orders = _yaml(workspace, "segments/core.yml")["segments"]["big_orders"]
    assert (
        big_orders["id"] == "segment.shop.large_orders" and "preview_dimensions" not in big_orders
    )
