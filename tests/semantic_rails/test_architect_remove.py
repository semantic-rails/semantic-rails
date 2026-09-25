"""remove_object: what goes, what is refused, the archive, and the impact report."""

from __future__ import annotations

import asyncio
import shutil
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

JAFFLE = Path(__file__).resolve().parents[2] / "configs" / "semantic_rails" / "jaffle_shop"


def _metric(measure: str) -> dict[str, Any]:
    return {
        "label": measure.title(),
        "kind": "aggregate",
        "measure": measure,
        "value_type": "count",
    }


def _dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Orders and customers; metrics on both; a segment on order status; an example."""
    shop = write_orders_package(tmp_path, seed={"kind": "external"})
    metrics = yaml.safe_load((shop / "metrics" / "core.yml").read_text(encoding="utf-8"))
    metrics["metrics"]["gross"] = _metric("order_total")
    _dump(shop / "metrics" / "core.yml", metrics)
    _dump(shop / "metrics" / "customers.yml", {"metrics": {"customers": _metric("customer_count")}})
    _dump(
        shop / "segments" / "core.yml",
        {
            "segments": {
                "big": {
                    "label": "Big orders",
                    "entity": "entity.shop_order",
                    "basis_metric": "metric.shop.revenue",
                    "membership": {
                        "where": [
                            {"field": "dimension.shop_order_status", "op": "=", "value": "big"}
                        ]
                    },
                }
            }
        },
    )
    _dump(
        shop / "examples" / "core.yml",
        {
            "examples": {
                "gross": {
                    "question": "Gross",
                    "query": {
                        "version": 1,
                        "select": [{"expression": {"metric": "metric.shop.gross"}, "as": "gross"}],
                    },
                }
            }
        },
    )
    return tmp_path


def _project(workspace: Path) -> ArchitectProject:
    return ArchitectProject(workspace / "shop", workspace_root=workspace)


def _yaml(workspace: Path, relative: str) -> dict[str, Any]:
    return dict(yaml.safe_load((workspace / "shop" / relative).read_text(encoding="utf-8")) or {})


@pytest.mark.parametrize(
    ("kind", "key", "model", "code", "message"),
    [
        ("measure", "order_total", "", "INVALID_CONFIG", "leaves metric.shop.gross, metric.shop"),
        ("model", "customers", "", "INVALID_CONFIG", "leaves metric.shop.customers naming it"),
        ("measure", "nope", "", "OBJECT_NOT_FOUND", "not on any model"),
        ("dimension", "status", "customers", "OBJECT_NOT_FOUND", "not on model 'customers'"),
        ("relationship", "order", "", "OBJECT_NOT_FOUND", "not on any model"),  # its own entity
        ("widget", "x", "", "INVALID_CONFIG", "kind must be one of"),
    ],
)
def test_refusals(
    workspace: Path, kind: str, key: str, model: str, code: str, message: str
) -> None:
    before = project_revision(workspace / "shop")

    with pytest.raises(SemanticLayerError, match=message) as refused:
        _project(workspace).remove_object(kind=kind, key=key, model=model)

    assert refused.value.code == code
    assert project_revision(workspace / "shop") == before


@pytest.mark.parametrize(
    ("kind", "key", "model", "status", "files", "removed"),
    [
        # The segment filters on status: the parse gate rolls it back.
        ("dimension", "status", "", "rolled_back_after_parse_error", ["models/orders.yml"], None),
        ("segment", "big", "", "removed", ["segments/core.yml"], [("segment", "big")]),
        (
            "relationship",
            "customer",
            "orders",
            "removed",
            ["models/orders.yml"],
            [("relationship", "customer")],
        ),
        (
            "time",
            "signed_up_on",
            "",
            "removed",
            ["models/customers.yml"],
            [("time", "signed_up_on")],
        ),
    ],
)
def test_removals(
    workspace: Path,
    kind: str,
    key: str,
    model: str,
    status: str,
    files: list[str],
    removed: list[tuple[str, str]] | None,
) -> None:
    report = _project(workspace).remove_object(kind=kind, key=key, model=model).report

    assert report["status"] == status, report
    archive = report["archived_to"]
    assert sorted(report["changed_files"]) == sorted([*files, archive])
    if removed is None:
        return
    assert [(row["kind"], row["key"]) for row in report["removed"]] == removed
    assert yaml.safe_load((workspace / "shop" / archive).read_text())["removed"][0]["key"] == key
    assert load_package_config(str(workspace / "shop"))


def test_a_model_goes_with_its_entity_and_the_references_to_it(workspace: Path) -> None:
    project = _project(workspace)
    project.remove_object(kind="metric", key="customers")
    before = {path: path.read_bytes() for path in (workspace / "shop").rglob("*.yml")}
    orders_fields = list(_yaml(workspace, "models/orders.yml")["model"])

    mutation = project.remove_object(kind="model", key="customers", reason="retired")

    report = mutation.report
    assert report["ok"] is True, report
    assert [(row["kind"], row["key"]) for row in report["removed"]] == [
        ("model", "customers"),
        ("dimension", "customer_country"),
        ("time", "signed_up_on"),
        ("measure", "customer_count"),
        ("entity", "customer"),
        ("relationship", "customer"),
    ]
    assert not (workspace / "shop" / "models" / "customers.yml").exists()
    assert list(_yaml(workspace, "graph.yml")["graph"]["entities"]) == ["order"]
    orders = _yaml(workspace, "models/orders.yml")["model"]
    assert orders["entities"] == {"order": {}} and list(orders) == orders_fields
    archived = yaml.safe_load((workspace / "shop" / report["archived_to"]).read_text())
    assert archived["reason"] == "retired"
    assert archived["removed"][0]["spec"]["relation"] == "main_marts.dim_customers"

    assert mutation.undo()["ok"] is True
    assert {path: path.read_bytes() for path in (workspace / "shop").rglob("*.yml")} == before


def test_every_definition_goes_and_mentions_are_reported(workspace: Path) -> None:
    # A one-object file after metrics/core.yml overrides its gross.
    _dump(
        workspace / "shop" / "metrics" / "z.yml",
        {"metric": {"name": "gross", **_metric("order_count")}},
    )

    report = _project(workspace).remove_object(kind="metric", key="gross").report

    assert report["ok"] is True, report
    assert [(row["source_file"], row.get("shadowed", False)) for row in report["removed"]] == [
        ("metrics/core.yml", True),
        ("metrics/z.yml", False),
    ]
    assert not (workspace / "shop" / "metrics" / "z.yml").exists()
    assert list(_yaml(workspace, "metrics/core.yml")["metrics"]) == ["revenue"]
    assert report["impact"]["references"] == [
        {"file": "examples/core.yml", "ids": ["metric.shop.gross"]}
    ]
    assert report["impact"]["changes"] == [
        {"object_id": "metric.shop.gross", "kind": "metrics", "change_type": "removed"}
    ]


def test_mcp_preview_apply_replay_and_stale_revision(workspace: Path) -> None:
    server = create_architect_mcp_server(workspace_root=workspace)
    before = project_revision(workspace / "shop")
    arguments = {
        "project_path": "shop",
        "kind": "relationship",
        "key": "customer",
        "expected_revision": before,
    }

    async def run() -> list[dict[str, Any]]:
        async with create_connected_server_and_client_session(server) as session:

            async def call(**values: Any) -> dict[str, Any]:
                result = await session.call_tool("remove_object", {**arguments, **values})
                return dict(result.structuredContent or {})

            return [
                await call(idempotency_key="p", dry_run=True),
                await call(idempotency_key="a"),
                await call(idempotency_key="a"),
                await call(idempotency_key="b"),
            ]

    preview, applied, replayed, stale = asyncio.run(run())

    assert preview["status"] == "preview" and preview["changed_files"][-1] == "models/orders.yml"
    assert applied["status"] == "removed", applied
    assert applied["revision"] == preview["proposed_revision"]
    assert replayed["status"] == "replayed"
    assert stale["ok"] is False and "stale" in stale["error"]["message"]


def test_a_jaffle_preview_lists_the_examples_that_name_a_metric(tmp_path: Path) -> None:
    shutil.copytree(JAFFLE, tmp_path / "jaffle_shop")
    before = project_revision(tmp_path / "jaffle_shop")
    project = ArchitectProject(tmp_path / "jaffle_shop", workspace_root=tmp_path)

    report = project.remove_object(kind="metric", key="sales.aov_usd", dry_run=True).report

    assert report["status"] == "preview", report
    assert {"file": "examples/core.yml", "ids": ["metric.sales.aov_usd"]} in report["impact"][
        "references"
    ]
    assert report["impact"]["changes"] == [
        {"object_id": "metric.sales.aov_usd", "kind": "metrics", "change_type": "removed"}
    ]
    with pytest.raises(
        SemanticLayerError, match="leaves .*metric.sales.average_customer_lifetime_value"
    ):
        project.remove_object(kind="model", key="customers", dry_run=True)
    assert project_revision(tmp_path / "jaffle_shop") == before
