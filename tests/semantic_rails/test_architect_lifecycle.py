"""Remove objects with an impact preview, and replace models and metrics outright."""

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
from tests.semantic_rails.dbt_warehouse import build_dbt_warehouse, write_orders_package

CUSTOMER_COUNTRY = "dimension.shop_customer_customer_country"
ORDER_STATUS = "dimension.shop_order_status"


def _dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _yaml(path: Path) -> dict[str, Any]:
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def _orders_query(dimension: str) -> dict[str, Any]:
    return {
        "version": 1,
        "select": [{"expression": {"measure": "measure.shop.order_count"}, "as": "orders"}],
        "group_by": [dimension],
        "limit": 10,
    }


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Orders and customers over the dbt warehouse, with a segment, an example and a test."""
    package = write_orders_package(tmp_path, seed={"kind": "external"}, with_customers=True)
    build_dbt_warehouse(package / "data" / "warehouse.duckdb")
    _dump(
        package / "metrics" / "customers.yml",
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
        },
    )
    _dump(
        package / "segments" / "core.yml",
        {
            "segments": {
                "us_customers": {
                    "label": "US customers",
                    "description": "Customers in the US.",
                    "entity": "entity.shop_customer",
                    "basis_metric": "metric.shop.customer_count",
                    "membership": {
                        "where": [{"field": CUSTOMER_COUNTRY, "op": "=", "value": "US"}]
                    },
                }
            }
        },
    )
    _dump(
        package / "examples" / "core.yml",
        {
            "examples": {
                "orders_by_status": {
                    "question": "Orders by status",
                    "query": _orders_query(ORDER_STATUS),
                    "expected_shape": {"columns": [ORDER_STATUS, "orders"], "min_rows": 1},
                },
                "orders_by_country": {
                    "question": "Orders by customer country",
                    "query": _orders_query(CUSTOMER_COUNTRY),
                    "expected_shape": {"columns": [CUSTOMER_COUNTRY, "orders"], "min_rows": 1},
                },
            }
        },
    )
    _dump(
        package / "tests" / "core.yml",
        {
            "tests": {
                "order_status_columns": {
                    "kind": "query_returns_columns",
                    "query": _orders_query(ORDER_STATUS),
                    "columns": [ORDER_STATUS, "orders"],
                }
            }
        },
    )
    return tmp_path


def _project(workspace: Path) -> ArchitectProject:
    return ArchitectProject(workspace / "shop", workspace_root=workspace)


def _package(workspace: Path) -> Path:
    return workspace / "shop"


def test_the_fixture_package_is_whole(workspace: Path) -> None:
    config = load_package_config(str(_package(workspace)))
    assert {segment.id for segment in config.segments} == {"segment.shop.us_customers"}
    assert "relationship.orders_customer" in {row.id for row in config.relationships}
    impact = _project(workspace)._change_impact([], [])
    assert impact["broken"] == []  # everything compiles


def test_mcp_session_previews_then_removes_a_dimension_and_its_checks(workspace: Path) -> None:
    server = create_architect_mcp_server(workspace_root=workspace)
    remove = {"project_path": "shop", "kind": "dimension", "key": "status"}

    async def run() -> list[dict[str, Any]]:
        async with create_connected_server_and_client_session(server) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            annotations = tools["remove_object"].annotations
            assert annotations is not None and annotations.destructiveHint is True

            async def call(name: str, **arguments: Any) -> dict[str, Any]:
                return dict((await session.call_tool(name, arguments)).structuredContent or {})

            revision = project_revision(_package(workspace))
            preview = await call(
                "remove_object",
                **remove,
                expected_revision=revision,
                idempotency_key="preview",
                dry_run=True,
            )
            results = [preview]
            for kind, key in (("example", "orders_by_status"), ("test", "order_status_columns")):
                removed = await call(
                    "remove_object",
                    project_path="shop",
                    kind=kind,
                    key=key,
                    expected_revision=revision,
                    idempotency_key=f"remove-{key}",
                )
                results.append(removed)
                revision = removed["revision"]
            results.append(
                await call(
                    "remove_object",
                    **remove,
                    reason="Placeholder",
                    expected_revision=revision,
                    idempotency_key="remove-status",
                )
            )
            results.append(await call("validate_project", project_path="shop", mode="examples"))
            return results

    preview, example, test, dimension, examples = asyncio.run(run())

    assert preview["ok"] is True and preview["dry_run"] is True
    broken = {row["id"]: row["kind"] for row in preview["impact"]["broken"]}
    assert broken == {"example.orders_by_status": "example", "test.order_status_columns": "test"}
    assert {row["file"] for row in preview["impact"]["references"]} == {
        "examples/core.yml",
        "tests/core.yml",
    }
    assert example["ok"] is True and test["ok"] is True, (example, test)
    assert not (_package(workspace) / "tests" / "core.yml").exists()  # its only test
    assert dimension["ok"] is True, dimension
    assert dimension["impact"]["broken"] == [] and dimension["impact"]["references"] == []
    assert examples["ok"] is True, examples
    orders = _yaml(_package(workspace) / "models" / "orders.yml")["model"]
    assert "dimensions" not in orders  # status was its only dimension
    archived = _yaml(_package(workspace) / dimension["archived_to"])
    assert archived["reason"] == "Placeholder"
    assert archived["removed"][0]["spec"] == {"label": "Order Status", "kind": "categorical"}


@pytest.mark.parametrize(
    ("kind", "key", "broken"),
    [
        ("measure", "customer_count", "metric metric.shop.customer_count"),
        ("measure", "order_total", "metric metric.shop.revenue"),
        ("dimension", "customer_country", "segment segment.shop.us_customers"),
        ("metric", "customer_count", "segment segment.shop.us_customers"),
    ],
)
def test_a_removal_that_breaks_a_definition_is_refused(
    workspace: Path, kind: str, key: str, broken: str
) -> None:
    before = project_revision(_package(workspace))

    with pytest.raises(SemanticLayerError, match="remove or change those first") as refused:
        _project(workspace).remove_object(kind=kind, key=key)

    assert broken in str(refused.value)
    assert project_revision(_package(workspace)) == before


def test_removing_a_model_removes_its_entity_and_relationships(workspace: Path) -> None:
    project = _project(workspace)
    for kind, key in (
        ("segment", "us_customers"),
        ("metric", "customer_count"),
        ("example", "orders_by_country"),
    ):
        assert project.remove_object(kind=kind, key=key).report["ok"] is True

    mutation = project.remove_object(kind="model", key="customers")

    assert mutation.report["ok"] is True, mutation.report
    removed = {(row["kind"], row["key"]) for row in mutation.report["removed"]}
    assert removed == {
        ("model", "customers"),
        ("dimension", "customer_country"),
        ("time", "signed_up_on"),
        ("measure", "customer_count"),
        ("entity", "customer"),
        ("relationship", "orders_customer"),
    }
    assert not (_package(workspace) / "models" / "customers.yml").exists()
    graph = _yaml(_package(workspace) / "graph.yml")["graph"]
    assert set(graph["entities"]) == {"order"}
    orders = _yaml(_package(workspace) / "models" / "orders.yml")["model"]
    assert orders["entities"] == {"order": {}}
    config = load_package_config(str(_package(workspace)))
    assert not config.relationships
    assert not (_package(workspace) / "segments" / "core.yml").exists()


def test_removing_a_relationship_by_its_inferred_or_explicit_name(workspace: Path) -> None:
    project = _project(workspace)
    ruled = project.upsert_relationship(
        from_entity="order", to_entity="customer", columns=["customer_id"], safety="safe"
    )
    assert ruled.report["relationship"]["override"] is True
    project.remove_object(kind="example", key="orders_by_country")
    project.remove_object(kind="segment", key="us_customers")

    mutation = project.remove_object(kind="relationship", key="orders_customer")

    assert mutation.report["ok"] is True, mutation.report
    assert [row["source_file"] for row in mutation.report["removed"]] == [
        "graph.relationships",
        "models/orders.yml",
    ]
    assert "relationships" not in _yaml(_package(workspace) / "graph.yml")["graph"]
    assert (
        "customer" not in _yaml(_package(workspace) / "models" / "orders.yml")["model"]["entities"]
    )
    assert not load_package_config(str(_package(workspace))).relationships


def test_a_key_on_several_models_needs_the_model(workspace: Path) -> None:
    project = _project(workspace)
    project.upsert_model(
        model_id="customers",
        entity_key="customer",
        relation="main_marts.dim_customers",
        primary_key=["customer_id"],
        dimensions={"status": {"label": "Customer status", "kind": "categorical"}},
    )

    with pytest.raises(SemanticLayerError, match=r"on several models \(customers, orders\)"):
        project.remove_object(kind="dimension", key="status")
    mutation = project.remove_object(kind="dimension", key="status", model="customers")

    assert mutation.report["ok"] is True, mutation.report
    assert _yaml(_package(workspace) / "models" / "orders.yml")["model"]["dimensions"] == {
        "status": {"label": "Order Status", "kind": "categorical"}
    }


@pytest.mark.parametrize(
    ("arguments", "code", "message"),
    [
        ({"kind": "widget", "key": "x"}, "INVALID_CONFIG", "kind must be one of"),
        ({"kind": "metric", "key": ""}, "INVALID_CONFIG", "key is required"),
        ({"kind": "metric", "key": "margin"}, "OBJECT_NOT_FOUND", "not in this package"),
        ({"kind": "relationship", "key": "orders_store"}, "OBJECT_NOT_FOUND", "not in this"),
        ({"kind": "dimension", "key": "status", "model": "customers"}, "OBJECT_NOT_FOUND", "on"),
    ],
)
def test_unknown_objects_are_refused(
    workspace: Path, arguments: dict[str, Any], code: str, message: str
) -> None:
    with pytest.raises(SemanticLayerError, match=message) as refused:
        _project(workspace).remove_object(**arguments)
    assert refused.value.code == code


def test_removing_the_last_metric_is_rolled_back(workspace: Path) -> None:
    project = _project(workspace)
    project.remove_object(kind="segment", key="us_customers")
    assert project.remove_object(kind="metric", key="customer_count").report["ok"] is True
    before = project_revision(_package(workspace))

    mutation = project.remove_object(kind="metric", key="revenue")

    assert mutation.report["ok"] is False
    assert "must declare metric recipes" in str(mutation.report["errors"])
    assert project_revision(_package(workspace)) == before


def test_remove_object_dry_run_apply_and_undo(workspace: Path) -> None:
    project = _project(workspace)
    before = project_revision(_package(workspace))

    preview = project.remove_object(kind="example", key="orders_by_country", dry_run=True)
    assert preview.report["ok"] is True, preview.report
    assert project_revision(_package(workspace)) == before

    applied = project.remove_object(kind="example", key="orders_by_country")
    assert applied.report["ok"] is True, applied.report
    assert applied.report["revision"] == preview.report["proposed_revision"]
    assert (_package(workspace) / applied.report["archived_to"]).exists()

    assert applied.undo()["ok"] is True
    assert project_revision(_package(workspace)) == before
    assert "orders_by_country" in _yaml(_package(workspace) / "examples" / "core.yml")["examples"]


def test_upsert_model_replace_rewrites_the_model(workspace: Path) -> None:
    project = _project(workspace)
    project.remove_object(kind="example", key="orders_by_status")
    project.remove_object(kind="test", key="order_status_columns")
    orders = {
        "model_id": "orders",
        "entity_key": "order",
        "relation": "main_marts.fct_orders",
        "primary_key": ["order_id"],
        "measures": {
            "order_count": {
                "label": "Order Count",
                "kind": "entity_count",
                "entity_key": "order_id",
                "accumulation": {"kind": "event"},
                "value_type": "count",
            },
            "order_total": {
                "label": "Order Total",
                "kind": "aggregate",
                "expr": "order_total",
                "default_agg": "sum",
                "accumulation": {"kind": "flow"},
                "value_type": "currency",
            },
        },
    }

    with pytest.raises(SemanticLayerError, match="breaks metric metric.shop.revenue"):
        project.upsert_model(**{**orders, "measures": {}}, replace=True)
    mutation = project.upsert_model(**orders, replace=True, label="Orders mart")

    assert mutation.report["ok"] is True, mutation.report
    assert {(row["kind"], row["key"]) for row in mutation.report["dropped"]} == {
        ("dimension", "status"),
        ("time", "ordered_at"),
    }
    model = _yaml(_package(workspace) / "models" / "orders.yml")["model"]
    assert set(model) == {"id", "entities", "relation", "description", "label", "measures"}
    assert model["entities"] == {"order": {}, "customer": {}}  # relationships are kept


def test_mcp_upserts_take_replace_and_label(workspace: Path) -> None:
    server = create_architect_mcp_server(workspace_root=workspace)
    metric = {
        "label": "Revenue",
        "kind": "aggregate",
        "measure": "order_total",
        "value_type": "currency",
    }

    async def run() -> list[dict[str, Any]]:
        async with create_connected_server_and_client_session(server) as session:
            revision = project_revision(_package(workspace))
            replaced = dict(
                (
                    await session.call_tool(
                        "upsert_metric",
                        {
                            "project_path": "shop",
                            "metric_key": "revenue",
                            "spec": metric,
                            "replace": True,
                            "expected_revision": revision,
                            "idempotency_key": "metric",
                        },
                    )
                ).structuredContent
                or {}
            )
            labelled = dict(
                (
                    await session.call_tool(
                        "upsert_model",
                        {
                            "project_path": "shop",
                            "model_id": "customers",
                            "entity_key": "customer",
                            "relation": "main_marts.dim_customers",
                            "primary_key": ["customer_id"],
                            "label": "Customer mart",
                            "expected_revision": replaced["revision"],
                            "idempotency_key": "model",
                        },
                    )
                ).structuredContent
                or {}
            )
            return [replaced, labelled]

    replaced, labelled = asyncio.run(run())

    assert replaced["ok"] is True and labelled["ok"] is True, (replaced, labelled)
    metrics = _yaml(_package(workspace) / "metrics" / "core.yml")["metrics"]
    assert metrics["revenue"] == metric  # the description is gone
    customers = _yaml(_package(workspace) / "models" / "customers.yml")["model"]
    assert customers["label"] == "Customer mart"
