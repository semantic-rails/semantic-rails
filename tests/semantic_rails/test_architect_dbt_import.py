"""Import dbt models into a package: one transaction, with foreign keys as references."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.architect_transactions import project_revision
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from tests.semantic_rails.dbt_warehouse import (
    build_dbt_warehouse,
    write_dbt_artifacts,
    write_orders_package,
)

MARTS = ["dim_customers", "dim_stores", "dim_products", "fct_orders", "fct_order_lines"]


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """A strict package over the dbt warehouse, and the dbt project's target/."""
    package = write_orders_package(tmp_path, seed={"kind": "external"}, with_customers=False)
    build_dbt_warehouse(package / "data" / "warehouse.duckdb")
    write_dbt_artifacts(package / "data" / "warehouse.duckdb", tmp_path / "dbt" / "target")
    return tmp_path


def _calls(server: Any, calls: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    async def run() -> list[dict[str, Any]]:
        async with create_connected_server_and_client_session(server) as session:
            return [
                dict((await session.call_tool(name, arguments)).structuredContent or {})
                for name, arguments in calls
            ]

    return asyncio.run(run())


def _model(path: Path) -> dict[str, Any]:
    return dict(yaml.safe_load(path.read_text(encoding="utf-8"))["model"])


def test_mcp_session_imports_the_marts_as_a_joined_star(workspace: Path) -> None:
    server = create_architect_mcp_server(workspace_root=workspace)
    base = {"project_path": "shop", "target_dir": "dbt/target", "select": MARTS}
    revision = project_revision(workspace / "shop")

    preview, imported, runtime = _calls(
        server,
        [
            (
                "import_dbt_project",
                {**base, "expected_revision": revision, "idempotency_key": "p", "dry_run": True},
            ),
            ("import_dbt_project", {**base, "expected_revision": revision, "idempotency_key": "i"}),
            ("validate_project", {"project_path": "shop", "mode": "runtime"}),
        ],
    )

    assert preview["ok"] is True and preview["dry_run"] is True
    assert imported["ok"] is True, imported
    references = {(row["model"], row["entity"]) for row in imported["references"]}
    assert references == {
        ("orders", "customer"),
        ("orders", "store"),
        ("order_lines", "order"),
        ("order_lines", "product"),
    }
    assert imported["skipped_references"] == [] and imported["skipped_models"] == []
    assert runtime["ok"] is True, runtime
    lines = _model(workspace / "shop" / "models" / "dbt" / "order_lines.yml")
    assert lines["relation"] == "main_marts.fct_order_lines"
    assert set(lines["entities"]) == {"order_line", "order", "product"}

    engine = Runtime.from_path(str(workspace / "shop"))
    try:
        rows = engine.query(
            {
                "version": 1,
                "select": [{"expression": {"measure": "measure.shop.order_count"}, "as": "orders"}],
                "group_by": ["dimension.shop_customer_customer_country"],
                "order_by": [{"field": "dimension.shop_customer_customer_country"}],
                "limit": 10,
            }
        )["rows"]
    finally:
        engine.close()
    assert [(row["dimension.shop_customer_customer_country"], row["orders"]) for row in rows] == [
        ("GB", 3),
        ("NL", 1),
        ("US", 4),
    ]


def test_a_dry_run_import_writes_nothing(workspace: Path) -> None:
    server = create_architect_mcp_server(workspace_root=workspace)
    before = project_revision(workspace / "shop")

    (preview,) = _calls(
        server,
        [
            (
                "import_dbt_project",
                {
                    "project_path": "shop",
                    "target_dir": "dbt/target",
                    "select": MARTS,
                    "expected_revision": before,
                    "idempotency_key": "p",
                    "dry_run": True,
                },
            )
        ],
    )

    assert preview["ok"] is True
    assert {change["path"] for change in preview["changes"]} >= {
        "models/dbt/customers.yml",
        "models/dbt/order_lines.yml",
    }
    assert project_revision(workspace / "shop") == before


def test_models_without_a_dbt_key_are_reported_not_imported(workspace: Path) -> None:
    server = create_architect_mcp_server(workspace_root=workspace)

    (result,) = _calls(
        server,
        [
            (
                "import_dbt_project",
                {
                    "project_path": "shop",
                    "target_dir": "dbt/target",
                    "select": ["dim_customers", "stg_orders"],
                    "expected_revision": project_revision(workspace / "shop"),
                    "idempotency_key": "k",
                },
            )
        ],
    )

    assert result["ok"] is True, result
    assert [row["relation"] for row in result["skipped_models"]] == ["main_staging.stg_orders"]
    assert "no key in dbt" in result["skipped_models"][0]["reason"]


def _customers(**extra: Any) -> dict[str, Any]:
    return {
        "model_id": "customers",
        "entity_key": "customer",
        "relation": "main_marts.dim_customers",
        "primary_key": ["customer_id"],
        **extra,
    }


def test_references_resolve_by_entity_or_relation_and_otherwise_are_reported(
    workspace: Path,
) -> None:
    project = ArchitectProject(workspace / "shop", workspace_root=workspace)

    mutation = project.upsert_models(
        [
            _customers(),
            {
                "model_id": "lines",
                "entity_key": "line",
                "relation": "main_marts.fct_order_lines",
                "primary_key": ["order_id", "line_number"],
                "references": [
                    {"relation": "main_marts.fct_orders", "columns": ["order_id"]},
                    {"entity": "customer", "columns": ["buyer_id"]},
                    {"relation": "main_marts.dim_products", "columns": ["product_id"]},
                    {"entity": "order", "columns": ["order_id"], "to_columns": ["order_ref"]},
                ],
            },
        ]
    )

    report = mutation.report
    assert {(row["entity"], tuple(row["columns"])) for row in report["references"]} == {
        ("order", ("order_id",)),
        ("customer", ("buyer_id",)),
    }
    reasons = [row["reason"] for row in report["skipped_references"]]
    assert any("not a model in this package" in reason for reason in reasons)  # products
    assert any("not order's key" in reason for reason in reasons)
    lines = _model(workspace / "shop" / "models" / "core" / "lines.yml")
    assert lines["entities"]["customer"] == {"expr": "buyer_id"}  # a differently named key
    assert lines["entities"]["order"] == {}


def test_a_batch_is_one_transaction(workspace: Path) -> None:
    project = ArchitectProject(workspace / "shop", workspace_root=workspace)
    before = project_revision(workspace / "shop")

    mutation = project.upsert_models(
        [
            _customers(),
            {
                "model_id": "stores",
                "entity_key": "store",
                "relation": "main_marts.dim_stores",
                "primary_key": ["store_id"],
                "measures": {"broken": {"kind": "no_such_kind"}},
            },
        ]
    )

    assert mutation.report["ok"] is False
    assert project_revision(workspace / "shop") == before
    assert not (workspace / "shop" / "models" / "core" / "customers.yml").exists()


def test_one_batch_cannot_repeat_a_model(workspace: Path) -> None:
    project = ArchitectProject(workspace / "shop", workspace_root=workspace)

    with pytest.raises(SemanticLayerError, match="repeated: customers"):
        project.upsert_models([_customers(), _customers(entity_key="client")])


def test_import_revision_and_idempotency_are_enforced_at_the_mcp_boundary(
    workspace: Path,
) -> None:
    server = create_architect_mcp_server(workspace_root=workspace)
    before = project_revision(workspace / "shop")
    base = {
        "project_path": "shop",
        "target_dir": "dbt/target",
        "select": ["dim_customers"],
        "expected_revision": before,
    }
    applied, replayed, stale, reused = _calls(
        server,
        [
            ("import_dbt_project", {**base, "idempotency_key": "one"}),
            ("import_dbt_project", {**base, "idempotency_key": "one"}),
            ("import_dbt_project", {**base, "idempotency_key": "two"}),
            (
                "import_dbt_project",
                {**base, "select": ["dim_stores"], "idempotency_key": "one"},
            ),
        ],
    )

    assert applied["ok"] is True and applied["revision"] != before
    assert replayed["ok"] is True and replayed["idempotent_replay"] is True
    assert replayed["revision"] == applied["revision"]
    assert stale["ok"] is False and stale["error"]["details"]["conflict_kind"] == "stale_revision"
    assert reused["ok"] is False
    assert reused["error"]["details"]["conflict_kind"] == "idempotency_key_reuse"


def _contract_manifest(
    workspace: Path, *, column_level: bool, to: str = "ref('dim_customers')"
) -> None:
    path = workspace / "dbt" / "target" / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    nodes = manifest["nodes"]
    for unique_id, node in list(nodes.items()):
        if (
            node.get("resource_type") == "test"
            and node.get("attached_node") == "model.shop_dbt.fct_orders"
            and node.get("column_name") == "customer_id"
            and node.get("test_metadata", {}).get("name") == "relationships"
        ):
            del nodes[unique_id]
    constraint = {"type": "foreign_key", "to": to, "to_columns": ["customer_id"]}
    orders = nodes["model.shop_dbt.fct_orders"]
    if column_level:
        orders.setdefault("columns", {}).setdefault(
            "customer_id", {"name": "customer_id"}
        ).setdefault("constraints", []).append(constraint)
    else:
        orders.setdefault("constraints", []).append({**constraint, "columns": ["customer_id"]})
    path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize("column_level", [False, True], ids=["model", "column"])
def test_mcp_import_applies_a_resolved_contract_foreign_key(
    workspace: Path, column_level: bool
) -> None:
    _contract_manifest(workspace, column_level=column_level)
    server = create_architect_mcp_server(workspace_root=workspace)
    (imported,) = _calls(
        server,
        [
            (
                "import_dbt_project",
                {
                    "project_path": "shop",
                    "target_dir": "dbt/target",
                    "select": ["dim_customers", "fct_orders"],
                    "expected_revision": project_revision(workspace / "shop"),
                    "idempotency_key": "contract-fk",
                },
            )
        ],
    )

    assert imported["ok"] is True, imported
    assert {"model": "orders", "entity": "customer", "columns": ["customer_id"]} in imported[
        "references"
    ]
    assert not any(
        row.get("relation") == "ref('dim_customers')" for row in imported["skipped_references"]
    )
    assert "customer" in _model(workspace / "shop" / "models" / "orders.yml")["entities"]


def test_unresolved_contract_reference_is_reported_instead_of_dropped(workspace: Path) -> None:
    _contract_manifest(workspace, column_level=False, to="ref('missing_customers')")
    server = create_architect_mcp_server(workspace_root=workspace)
    (preview,) = _calls(
        server,
        [
            (
                "import_dbt_project",
                {
                    "project_path": "shop",
                    "target_dir": "dbt/target",
                    "select": ["fct_orders"],
                    "expected_revision": project_revision(workspace / "shop"),
                    "idempotency_key": "unknown-fk",
                    "dry_run": True,
                },
            )
        ],
    )

    assert preview["ok"] is True, preview
    missing = [
        row
        for row in preview["skipped_references"]
        if row.get("relation") == "ref('missing_customers')"
    ]
    assert len(missing) == 1
    assert "not a model in this package" in missing[0]["reason"]


def test_contract_composite_width_and_target_column_mismatches_are_reported(
    workspace: Path,
) -> None:
    _contract_manifest(workspace, column_level=False)
    path = workspace / "dbt" / "target" / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    orders_constraint = manifest["nodes"]["model.shop_dbt.fct_orders"]["constraints"][-1]
    orders_constraint["to_columns"] = ["wrong_key"]
    manifest["nodes"]["model.shop_dbt.fct_order_lines"].setdefault("constraints", []).append(
        {
            "type": "foreign_key",
            "columns": ["order_id", "line_number"],
            "to": "ref('fct_orders')",
            "to_columns": ["order_id"],
        }
    )
    path.write_text(json.dumps(manifest), encoding="utf-8")
    server = create_architect_mcp_server(workspace_root=workspace)
    (preview,) = _calls(
        server,
        [
            (
                "import_dbt_project",
                {
                    "project_path": "shop",
                    "target_dir": "dbt/target",
                    "select": ["dim_customers", "fct_orders", "fct_order_lines"],
                    "expected_revision": project_revision(workspace / "shop"),
                    "idempotency_key": "composite-mismatch",
                    "dry_run": True,
                },
            )
        ],
    )

    assert preview["ok"] is True, preview
    skipped = preview["skipped_references"]
    assert any(
        row.get("columns") == ["order_id", "line_number"] and "width" in row["reason"]
        for row in skipped
    )
    assert any(
        row.get("to_columns") == ["wrong_key"] and "not customer's key" in row["reason"]
        for row in skipped
    )
