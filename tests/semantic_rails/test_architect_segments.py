"""Segments the engine can validate, and metrics that share a file."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.architect_transactions import project_revision
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from tests.semantic_rails.dbt_warehouse import build_dbt_warehouse, write_orders_package

COUNTRY = "dimension.shop_customer_customer_country"
US_ONLY = {"where": [{"field": COUNTRY, "op": "=", "value": "US"}]}


def _segment(**fields: Any) -> dict[str, Any]:
    return {
        "label": "US customers",
        "description": "Customers in the US.",
        "entity": "entity.shop_customer",
        "basis_metric": "metric.shop.customer_count",
        "membership": US_ONLY,
        **fields,
    }


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Orders and customers over the dbt warehouse, with a customer count metric."""
    package = write_orders_package(tmp_path, seed={"kind": "external"}, with_customers=True)
    build_dbt_warehouse(package / "data" / "warehouse.duckdb")
    (package / "metrics" / "customers.yml").write_text(
        yaml.safe_dump(
            {
                "metrics": {
                    "customer_count": {
                        "label": "Customers",
                        "description": "Customers.",
                        "kind": "aggregate",
                        "measure": "customer_count",
                        "value_type": "count",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _project(workspace: Path) -> ArchitectProject:
    return ArchitectProject(workspace / "shop", workspace_root=workspace)


def _yaml(path: Path) -> dict[str, Any]:
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def _members(workspace: Path, segment_id: str) -> int:
    engine = Runtime.from_path(str(workspace / "shop"))
    try:
        return int(engine.segment_preview(segment_id)["member_count"])
    finally:
        engine.close()


def test_mcp_session_writes_a_segment_that_selects_its_members(workspace: Path) -> None:
    server = create_architect_mcp_server(workspace_root=workspace)
    arguments = {
        "project_path": "shop",
        "segment_key": "us_customers",
        "spec": _segment(),
        "expected_revision": project_revision(workspace / "shop"),
    }

    async def run() -> list[dict[str, Any]]:
        async with create_connected_server_and_client_session(server) as session:

            async def call(name: str, **values: Any) -> dict[str, Any]:
                return dict((await session.call_tool(name, values)).structuredContent or {})

            preview = await call("upsert_segment", **arguments, idempotency_key="p", dry_run=True)
            applied = await call("upsert_segment", **arguments, idempotency_key="a")
            empty = await call(
                "upsert_segment",
                **{
                    **arguments,
                    "segment_key": "everyone",
                    "spec": _segment(membership=None),
                    "expected_revision": applied["revision"],
                },
                idempotency_key="m",
            )
            return [preview, applied, empty]

    preview, applied, empty = asyncio.run(run())

    assert preview["ok"] is True and preview["changed_files"] == ["segments/core.yml"]
    assert applied["ok"] is True, applied
    assert _members(workspace, "segment.shop.us_customers") == 3
    assert empty["ok"] is False
    assert "needs membership" in empty["error"]["message"]


def test_membership_fields_outside_membership_are_refused(workspace: Path) -> None:
    """B2 hit this: a top-level where parses, and the segment selects everyone."""
    spec = {key: value for key, value in _segment().items() if key != "membership"}

    with pytest.raises(SemanticLayerError, match="where belong under membership:"):
        _project(workspace).upsert_segment(segment_key="us", spec={**spec, **US_ONLY})


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (_segment(colour="blue"), "unknown fields: colour"),
        (_segment(membership={**US_ONLY, "having": []}), "unknown fields: membership.having"),
        (_segment(membership={"time": {}}), "needs membership: where, metric_filters or time"),
        (_segment(basis_metric="metric.shop.refunds"), "does not validate"),
        (
            _segment(membership={"where": [{"field": "dimension.shop_x", "op": "=", "value": 1}]}),
            "does not validate",
        ),
    ],
)
def test_segments_the_engine_cannot_use_are_refused(
    workspace: Path, spec: dict[str, Any], message: str
) -> None:
    before = project_revision(workspace / "shop")

    with pytest.raises(SemanticLayerError, match=message):
        _project(workspace).upsert_segment(segment_key="us_customers", spec=spec)

    assert project_revision(workspace / "shop") == before


def test_segments_merge_unless_replaced(workspace: Path) -> None:
    project = _project(workspace)
    project.upsert_segment(segment_key="us_customers", spec=_segment(preview_dimensions=[COUNTRY]))

    project.upsert_segment(segment_key="us_customers", spec={"label": "American customers"})
    merged = _yaml(workspace / "shop" / "segments" / "core.yml")["segments"]["us_customers"]
    project.upsert_segment(segment_key="us_customers", spec=_segment(), replace=True)
    replaced = _yaml(workspace / "shop" / "segments" / "core.yml")["segments"]["us_customers"]

    assert merged["label"] == "American customers" and merged["preview_dimensions"] == [COUNTRY]
    assert "preview_dimensions" not in replaced


def test_upsert_segment_dry_run_apply_and_undo(workspace: Path) -> None:
    project = _project(workspace)
    before = project_revision(workspace / "shop")

    preview = project.upsert_segment(segment_key="us", spec=_segment(), dry_run=True)
    assert preview.report["ok"] is True, preview.report
    assert project_revision(workspace / "shop") == before

    applied = project.upsert_segment(segment_key="us", spec=_segment())
    assert applied.report["revision"] == preview.report["proposed_revision"]
    assert applied.undo()["ok"] is True
    assert project_revision(workspace / "shop") == before


def test_metrics_can_share_a_file(workspace: Path) -> None:
    project = _project(workspace)
    total = {
        "label": "Order total",
        "kind": "aggregate",
        "measure": "order_total",
        "value_type": "currency",
    }
    orders = {
        "label": "Orders",
        "kind": "aggregate",
        "measure": "order_count",
        "value_type": "count",
    }

    for key, spec in (("order_value", total), ("orders", orders)):
        mutation = project.upsert_metric(metric_key=key, spec=spec, file_name="sales.yml")
        assert mutation.report["ok"] is True, mutation.report
    moved = project.upsert_metric(
        metric_key="customer_count", spec={"label": "Customer count"}, file_name="sales.yml"
    )

    assert set(_yaml(workspace / "shop" / "metrics" / "sales.yml")["metrics"]) == {
        "order_value",
        "orders",
    }
    assert moved.report["target_file"] == "metrics/customers.yml"  # it stays where it is
    assert not (workspace / "shop" / "metrics" / "core" / "orders.yml").exists()


def test_a_time_window_is_membership_enough(workspace: Path) -> None:
    signed_up = {
        "time": {
            "temporal_role": "temporal_role.shop_customer_signed_up_on",
            "start": "2024-01-01",
            "end": "2024-03-01",
        }
    }

    mutation = _project(workspace).upsert_segment(
        segment_key="early_customers", spec=_segment(membership=signed_up)
    )

    assert mutation.report["ok"] is True, mutation.report


def test_a_replace_keeps_the_public_id(workspace: Path) -> None:
    project = _project(workspace)
    project.upsert_segment(
        segment_key="us_customers", spec=_segment(**{"as": "segment.shop.american_customers"})
    )

    project.upsert_segment(segment_key="us_customers", spec=_segment(label="US"), replace=True)

    stored = _yaml(workspace / "shop" / "segments" / "core.yml")["segments"]["us_customers"]
    assert stored["as"] == "segment.shop.american_customers"
    assert _members(workspace, "segment.shop.american_customers") == 3


def test_a_one_metric_file_keeps_its_metric_when_another_joins_it(workspace: Path) -> None:
    package = workspace / "shop"
    gross = {
        "label": "Gross",
        "kind": "aggregate",
        "measure": "order_total",
        "value_type": "currency",
    }
    (package / "metrics" / "sales.yml").write_text(
        yaml.safe_dump({"metric": {"name": "gross", **gross}}), encoding="utf-8"
    )

    mutation = _project(workspace).upsert_metric(
        metric_key="orders",
        spec={
            "label": "Orders",
            "kind": "aggregate",
            "measure": "order_count",
            "value_type": "count",
        },
        file_name="sales.yml",
    )

    assert mutation.report["ok"] is True, mutation.report
    assert set(_yaml(package / "metrics" / "sales.yml")["metrics"]) == {"gross", "orders"}
    loaded = {metric.id for metric in load_package_config(str(package)).metric_recipes}
    assert {"metric.shop.gross", "metric.shop.orders"} <= loaded


def test_segments_are_checked_under_the_package_namespace(tmp_path: Path) -> None:
    package = write_orders_package(tmp_path, seed={"kind": "external"}, with_customers=True)
    build_dbt_warehouse(package / "data" / "warehouse.duckdb")
    document = _yaml(package / "package.yml")
    document["package"]["namespace"] = "retail"
    (package / "package.yml").write_text(yaml.safe_dump(document), encoding="utf-8")
    (package / "metrics" / "customers.yml").write_text(
        yaml.safe_dump(
            {
                "metrics": {
                    "customer_count": {
                        "label": "Customers",
                        "kind": "aggregate",
                        "measure": "customer_count",
                        "value_type": "count",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    retail = {
        "label": "US customers",
        "entity": "entity.retail_customer",
        "basis_metric": "metric.retail.customer_count",
        "membership": {
            "where": [
                {"field": "dimension.retail_customer_customer_country", "op": "=", "value": "US"}
            ]
        },
    }

    mutation = ArchitectProject(package, workspace_root=tmp_path).upsert_segment(
        segment_key="us_customers", spec=retail
    )

    assert mutation.report["ok"] is True, mutation.report
