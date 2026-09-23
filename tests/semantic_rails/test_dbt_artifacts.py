"""dbt manifest.json and catalog.json, read for package authoring (dbt never runs)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.dbt_artifacts import load_dbt_artifacts, suggest_models_from_dbt
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from tests.semantic_rails.dbt_warehouse import (
    build_dbt_warehouse,
    write_dbt_artifacts,
    write_orders_package,
)


@pytest.fixture()
def target(tmp_path: Path) -> Path:
    db_path = build_dbt_warehouse(tmp_path / "warehouse.duckdb")
    return write_dbt_artifacts(db_path, tmp_path / "target")


def _by(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {str(row[key]): row for row in rows}


def test_relations_keep_their_schema_and_alias(target: Path) -> None:
    project = load_dbt_artifacts(target)

    assert project.project_name == "shop_dbt" and project.adapter_type == "duckdb"
    assert project.find("fct_orders").relation == "main_marts.fct_orders"
    assert project.find("stg_orders").relation == "main_staging.stg_orders"
    assert project.find("seed.shop_dbt.raw_orders").relation == "main.raw_orders"
    assert project.find("fct_orders").materialized == "table"


def test_tests_and_contracts_become_keys_links_and_value_sets(target: Path) -> None:
    project = load_dbt_artifacts(target)

    orders = project.find("fct_orders")
    assert (orders.primary_key, orders.primary_key_source) == (
        ["order_id"],
        "unique and not_null tests",
    )
    assert project.find("dim_customers").primary_key_source == "contract"
    lines = project.find("fct_order_lines")
    assert lines.primary_key == ["order_id", "line_number"]
    assert {(fk["columns"][0], fk["to_relation"]) for fk in lines.foreign_keys} == {
        ("order_id", "main_marts.fct_orders"),
        ("product_id", "main_marts.dim_products"),
    }
    assert orders.columns["status"].accepted_values == [
        "placed",
        "shipped",
        "delivered",
        "returned",
    ]
    assert orders.columns["order_total"].description == "Order value after discounts."
    assert orders.columns["order_total"].data_type.startswith("DECIMAL")


def test_suggestions_prefer_dbt_facts_over_guesses(target: Path) -> None:
    (orders,) = suggest_models_from_dbt(load_dbt_artifacts(target), ["fct_orders"])

    assert orders["primary_key"]["reason"] == "dbt unique and not_null tests"
    status = _by(orders["dimensions"], "column")["status"]
    assert status["confidence"] == "high" and status["values"][0] == "placed"
    assert _by(orders["foreign_keys"], "column")["customer_id"]["reason"] == (
        "dbt relationships test"
    )
    assert _by(orders["times"], "column")["ordered_at"]["confidence"] == "high"  # not_null test
    draft = orders["upsert_model"]
    assert draft["relation"] == "main_marts.fct_orders"
    assert draft["dimensions"]["status"]["domain"] == ["placed", "shipped", "delivered", "returned"]
    assert draft["measures"]["order_total"]["description"] == "Order value after discounts."
    assert draft["description"] == "One row per order."


def test_by_default_every_model_and_no_seed_is_suggested(target: Path) -> None:
    suggestions = suggest_models_from_dbt(load_dbt_artifacts(target))

    assert len(suggestions) == 10
    assert all(row["dbt_unique_id"].startswith("model.") for row in suggestions)


def test_a_manifest_without_a_catalog_still_loads(target: Path) -> None:
    (target / "catalog.json").unlink()

    project = load_dbt_artifacts(target)
    (orders,) = suggest_models_from_dbt(project, ["fct_orders"])

    assert project.find("fct_orders").primary_key == ["order_id"]
    assert "ordered_at" in orders["untyped_columns"]  # no types without catalog.json


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda target: (target / "manifest.json").unlink(), "run `dbt build`"),
        (lambda target: (target / "manifest.json").write_text("{", encoding="utf-8"), "JSON"),
    ],
    ids=["missing", "corrupt"],
)
def test_missing_or_corrupt_artifacts_are_reported(target: Path, setup: Any, message: str) -> None:
    setup(target)

    with pytest.raises(SemanticLayerError, match=message):
        load_dbt_artifacts(target)


def test_an_unknown_or_ambiguous_model_is_reported(target: Path) -> None:
    project = load_dbt_artifacts(target)

    with pytest.raises(SemanticLayerError, match="not in the manifest"):
        project.find("fct_refunds")


def test_a_suggested_draft_runs_against_the_dbt_warehouse(tmp_path: Path, target: Path) -> None:
    package = write_orders_package(tmp_path, seed={"kind": "external"}, with_customers=False)
    build_dbt_warehouse(package / "data" / "warehouse.duckdb")
    (customers,) = suggest_models_from_dbt(load_dbt_artifacts(target), ["dim_customers"])

    mutation = ArchitectProject(package, workspace_root=tmp_path).upsert_model(
        **customers["upsert_model"]
    )
    assert mutation.report["ok"] is True, mutation.report
    runtime = Runtime.from_path(str(package))
    try:
        rows = runtime.query(
            {
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.shop.customer_count"}, "as": "customers"}
                ],
                "limit": 5,
            }
        )["rows"]
    finally:
        runtime.close()
    assert rows == [{"customers": 5}]


def _call(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        async with create_connected_server_and_client_session(server) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert tools[name].annotations is not None
            assert tools[name].annotations.readOnlyHint is True
            result = await session.call_tool(name, arguments)
            return dict(result.structuredContent or {})

    return asyncio.run(run())


def test_mcp_session_suggests_models_from_the_dbt_target(tmp_path: Path) -> None:
    db_path = build_dbt_warehouse(tmp_path / "dbt" / "warehouse.duckdb")
    write_dbt_artifacts(db_path, tmp_path / "dbt" / "target")
    server = create_architect_mcp_server(workspace_root=tmp_path)

    result = _call(
        server,
        "suggest_models_from_dbt",
        {"target_dir": "dbt/target", "select": ["fct_orders", "fct_order_lines"]},
    )

    assert result["ok"] is True, result
    assert result["dbt_project"] == "shop_dbt"
    models = _by(result["models"], "relation")
    assert models["main_marts.fct_order_lines"]["primary_key"]["columns"] == [
        "order_id",
        "line_number",
    ]


def test_mcp_dbt_paths_stay_inside_the_workspace(tmp_path: Path) -> None:
    db_path = build_dbt_warehouse(tmp_path / "outside" / "warehouse.duckdb")
    write_dbt_artifacts(db_path, tmp_path / "outside" / "target")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = create_architect_mcp_server(workspace_root=workspace)

    result = _call(
        server, "suggest_models_from_dbt", {"target_dir": str(tmp_path / "outside" / "target")}
    )

    assert result["ok"] is False and "workspace root" in result["error"]["message"]
    json.dumps(result)  # the error payload is serializable
