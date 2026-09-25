"""Metric and segment files: shared metric files, lone-object files, and segment membership."""

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
from tests.semantic_rails.dbt_warehouse import write_orders_package

COUNTRY = "dimension.shop_customer_customer_country"
US_ONLY = {"where": [{"field": COUNTRY, "op": "=", "value": "US"}]}
ORDERED = {  # customers with at least one order
    "metric_filters": [
        {
            "expression": {
                "kind": "metric_predicate",
                "entity": "entity.shop_customer",
                "scope_mode": "entity_only",
                "input": {"measure": "measure.shop.order_count"},
                "op": ">=",
                "value": 1,
            },
            "op": "=",
            "value": True,
        }
    ]
}
SIGNED_UP = {
    "time": {
        "temporal_role": "temporal_role.shop_customer_signed_up_on",
        "start": "2024-01-01",
        "end": "2024-03-01",
    }
}


def _segment(**fields: Any) -> dict[str, Any]:
    return {
        "label": "US customers",
        "entity": "entity.shop_customer",
        "basis_metric": "metric.shop.customer_count",
        "membership": US_ONLY,
        **fields,
    }


def _metric(measure: str) -> dict[str, Any]:
    return {
        "label": measure.title(),
        "kind": "aggregate",
        "measure": measure,
        "value_type": "count",
    }


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Orders and customers, with a customer count metric for segments to count by."""
    shop = write_orders_package(tmp_path, seed={"kind": "external"})
    (shop / "metrics" / "customers.yml").write_text(
        yaml.safe_dump({"metrics": {"customer_count": _metric("customer_count")}}),
        encoding="utf-8",
    )
    return tmp_path


def _project(workspace: Path) -> ArchitectProject:
    return ArchitectProject(workspace / "shop", workspace_root=workspace)


def _yaml(path: Path) -> dict[str, Any]:
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


@pytest.mark.parametrize(
    ("relative", "lone", "lone_key", "upsert"),
    [
        (
            "metrics/sales.yml",
            {"metric": {"name": "gross", **_metric("order_count")}},
            "gross",
            lambda project: project.upsert_metric(
                metric_key="net", spec=_metric("order_count"), file_name="sales.yml"
            ),
        ),
        (
            "metrics/sales.yml",
            _metric("order_count"),  # a bare mapping, known by its file name
            "sales",
            lambda project: project.upsert_metric(
                metric_key="net", spec=_metric("order_count"), file_name="sales.yml"
            ),
        ),
        (
            "segments/core.yml",
            {"segment": {"name": "early", **_segment(membership=SIGNED_UP)}},
            "early",
            lambda project: project.upsert_segment(segment_key="net", spec=_segment()),
        ),
    ],
)
def test_adding_beside_a_lone_object_keeps_it(
    workspace: Path, relative: str, lone: dict[str, Any], lone_key: str, upsert: Any
) -> None:
    path = workspace / "shop" / relative
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump(lone, sort_keys=False), encoding="utf-8")

    report = upsert(_project(workspace)).report

    assert report["ok"] is True, report
    plural = relative.split("/")[0]
    assert list(_yaml(path)[plural]) == [lone_key, "net"]
    config = load_package_config(str(workspace / "shop"))
    loaded = {row.id for row in [*config.metric_recipes, *config.segments]}
    kind = plural.rstrip("s")
    assert {f"{kind}.shop.{lone_key}", f"{kind}.shop.net"} <= loaded


@pytest.mark.parametrize(
    ("membership", "refused"),
    [
        (None, True),
        ({}, True),
        ({"where": []}, True),
        (US_ONLY, False),
        (ORDERED, False),
        (SIGNED_UP, False),
    ],
)
def test_a_segment_needs_membership_criteria(
    workspace: Path, membership: dict[str, Any] | None, refused: bool
) -> None:
    before = project_revision(workspace / "shop")
    spec = _segment(membership=membership)

    if refused:
        with pytest.raises(SemanticLayerError, match="needs membership"):
            _project(workspace).upsert_segment(segment_key="us", spec=spec)
        assert project_revision(workspace / "shop") == before
    else:
        report = _project(workspace).upsert_segment(segment_key="us", spec=spec).report
        assert report["ok"] is True, report


def test_mcp_metrics_can_share_a_file(workspace: Path) -> None:
    server = create_architect_mcp_server(workspace_root=workspace)

    async def run() -> list[dict[str, Any]]:
        async with create_connected_server_and_client_session(server) as session:
            reports = []
            for key, measure in (
                ("orders", "order_count"),
                ("sales", "order_total"),
                ("customer_count", "customer_count"),  # exists, so it stays in its file
            ):
                result = await session.call_tool(
                    "upsert_metric",
                    {
                        "project_path": "shop",
                        "metric_key": key,
                        "spec": _metric(measure),
                        "file_name": "sales.yml",
                        "expected_revision": project_revision(workspace / "shop"),
                        "idempotency_key": key,
                    },
                )
                reports.append(dict(result.structuredContent or {}))
            return reports

    reports = asyncio.run(run())

    assert [report["ok"] for report in reports] == [True, True, True], reports
    assert [report["target_file"] for report in reports] == [
        "metrics/sales.yml",
        "metrics/sales.yml",
        "metrics/customers.yml",
    ]
    assert list(_yaml(workspace / "shop" / "metrics" / "sales.yml")["metrics"]) == [
        "orders",
        "sales",
    ]
