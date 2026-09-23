"""Relate entities through key columns: the entities-block reference plus graph.relationships."""

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

CUSTOMER_COUNTRY = "dimension.shop_customer_customer_country"
STORE_COUNTRY = "dimension.shop_store_store_country"


def _dimension_model(model_id: str, entity: str, table: str, country: str) -> dict[str, Any]:
    return {
        "model_id": model_id,
        "entity_key": entity,
        "relation": f"main_marts.{table}",
        "primary_key": [f"{entity}_id"],
        "dimensions": {
            country: {"label": country.replace("_", " ").title(), "kind": "categorical"}
        },
    }


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Orders, customers and stores over the dbt warehouse, not yet related."""
    package = write_orders_package(tmp_path, seed={"kind": "external"}, with_customers=False)
    build_dbt_warehouse(package / "data" / "warehouse.duckdb")
    mutation = ArchitectProject(package, workspace_root=tmp_path).upsert_models(
        [
            _dimension_model("customers", "customer", "dim_customers", "customer_country"),
            _dimension_model("stores", "store", "dim_stores", "store_country"),
        ]
    )
    assert mutation.report["ok"] is True, mutation.report
    return tmp_path


def _project(workspace: Path) -> ArchitectProject:
    return ArchitectProject(workspace / "shop", workspace_root=workspace)


def _yaml(path: Path) -> dict[str, Any]:
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def _orders_entities(workspace: Path) -> dict[str, Any]:
    return dict(_yaml(workspace / "shop" / "models" / "orders.yml")["model"]["entities"])


def _graph_relationships(workspace: Path) -> dict[str, Any]:
    return dict(_yaml(workspace / "shop" / "graph.yml")["graph"].get("relationships") or {})


def _loaded(workspace: Path) -> dict[str, Any]:
    """Relationships as the engine loads them, by id."""
    config = load_package_config(str(workspace / "shop"))
    return {relationship.id: relationship for relationship in config.relationships}


def _orders_by(workspace: Path, dimension: str) -> list[tuple[Any, Any]]:
    engine = Runtime.from_path(str(workspace / "shop"))
    try:
        rows = engine.query(
            {
                "version": 1,
                "select": [{"expression": {"measure": "measure.shop.order_count"}, "as": "orders"}],
                "group_by": [dimension],
                "order_by": [{"field": dimension}],
                "limit": 10,
            }
        )["rows"]
    finally:
        engine.close()
    return [(row[dimension], row["orders"]) for row in rows]


def test_mcp_session_relates_orders_to_customers_and_stores(workspace: Path) -> None:
    server = create_architect_mcp_server(workspace_root=workspace)
    before = project_revision(workspace / "shop")

    async def run() -> list[dict[str, Any]]:
        async with create_connected_server_and_client_session(server) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            annotations = tools["upsert_relationship"].annotations
            assert annotations is not None and annotations.readOnlyHint is False

            async def call(name: str, **arguments: Any) -> dict[str, Any]:
                return dict((await session.call_tool(name, arguments)).structuredContent or {})

            order_customer = {
                "project_path": "shop",
                "from_entity": "order",
                "to_entity": "customer",
                "columns": ["customer_id"],
            }
            preview = await call(
                "upsert_relationship",
                **order_customer,
                expected_revision=before,
                idempotency_key="preview",
                dry_run=True,
            )
            customer = await call(
                "upsert_relationship",
                **order_customer,
                expected_revision=before,
                idempotency_key="customer",
            )
            store = await call(
                "upsert_relationship",
                project_path="shop",
                from_entity="order",
                to_entity="store",
                columns=["store_id"],
                path_preference=10,
                expected_revision=customer["revision"],
                idempotency_key="store",
            )
            runtime = await call("validate_project", project_path="shop", mode="runtime")
            return [preview, customer, store, runtime]

    preview, customer, store, runtime = asyncio.run(run())

    assert preview["ok"] is True and preview["dry_run"] is True
    assert [change["path"] for change in preview["changes"]] == ["models/orders.yml"]
    assert customer["ok"] is True, customer
    assert customer["relationship"]["id"] == "relationship.orders_customer"
    assert customer["relationship"]["override"] is False
    assert store["ok"] is True, store
    assert store["relationship"]["override"] is True
    assert runtime["ok"] is True, runtime
    assert _orders_entities(workspace) == {"order": {}, "customer": {}, "store": {}}
    assert _graph_relationships(workspace) == {
        "orders_store": {
            "entities": ["order", "store"],
            "path_preference": 10,
            "cardinality": "many_to_one",
        }
    }
    assert _orders_by(workspace, CUSTOMER_COUNTRY) == [("GB", 3), ("NL", 1), ("US", 4)]
    assert _orders_by(workspace, STORE_COUNTRY) == [("GB", 1), ("NL", 2), ("US", 5)]


def test_a_foreign_key_named_differently_from_the_key_is_written_with_expr(
    workspace: Path,
) -> None:
    mutation = _project(workspace).upsert_relationship(
        from_entity="order", to_entity="customer", columns=["buyer_id"]
    )

    assert mutation.report["ok"] is True, mutation.report
    assert _orders_entities(workspace)["customer"] == {"expr": "buyer_id"}
    relationship = _loaded(workspace)["relationship.orders_customer"]
    assert relationship.source_columns == ["buyer_id"]
    assert relationship.target_columns == ["customer_id"]
    assert (relationship.cardinality, relationship.safety) == ("N:1", "safe")
    assert _graph_relationships(workspace) == {}


def test_cardinality_and_options_write_a_graph_relationship(workspace: Path) -> None:
    project = _project(workspace)
    project.upsert_relationship(from_entity="order", to_entity="customer", columns=["customer_id"])
    before = set(_loaded(workspace))

    mutation = project.upsert_relationship(
        from_entity="order",
        to_entity="customer",
        columns=["customer_id"],
        cardinality="one_to_one",
        allowed_directions=["forward"],
        path_preference=10,
        label="Order customer",
    )

    assert mutation.report["ok"] is True, mutation.report
    assert _graph_relationships(workspace)["orders_customer"] == {
        "entities": ["order", "customer"],
        "allowed_directions": ["forward"],
        "path_preference": 10,
        "label": "Order customer",
        "cardinality": "one_to_one",
    }
    loaded = _loaded(workspace)
    assert set(loaded) == before  # the same relationship, now with rules
    relationship = loaded["relationship.orders_customer"]
    assert relationship.cardinality == "1:1"
    assert relationship.allowed_directions == ["forward"]
    assert (relationship.path_preference, relationship.label) == (10, "Order customer")


def test_one_to_many_is_recorded_on_the_many_side(workspace: Path) -> None:
    mutation = _project(workspace).upsert_relationship(
        from_entity="customer",
        to_entity="order",
        columns=["customer_id"],
        to_columns=["customer_id"],
        cardinality="one_to_many",
    )

    assert mutation.report["ok"] is True, mutation.report
    assert mutation.report["relationship"] == {
        "name": "orders_customer",
        "id": "relationship.orders_customer",
        "from_entity": "order",
        "to_entity": "customer",
        "columns": ["customer_id"],
        "to_columns": ["customer_id"],
        "cardinality": "many_to_one",
        "model": "orders",
        "override": False,
    }
    assert _orders_entities(workspace)["customer"] == {}
    customers = _yaml(workspace / "shop" / "models" / "core" / "customers.yml")["model"]
    assert set(customers["entities"]) == {"customer"}


def test_an_existing_entry_for_the_pair_is_updated_in_place(workspace: Path) -> None:
    project = _project(workspace)
    project.upsert_relationship(from_entity="order", to_entity="customer", columns=["customer_id"])
    graph_path = workspace / "shop" / "graph.yml"
    graph = _yaml(graph_path)
    graph["graph"]["relationships"] = {
        "order_to_customer": {
            "entities": ["order", "customer"],
            "cardinality": "many_to_one",
            "rollup_safe": {"reverse": ["count_distinct"]},
        }
    }
    graph_path.write_text(yaml.safe_dump(graph, sort_keys=False), encoding="utf-8")

    mutation = project.upsert_relationship(
        from_entity="order", to_entity="customer", columns=["customer_id"], safety="safe"
    )

    assert mutation.report["ok"] is True, mutation.report
    assert mutation.report["relationship"]["name"] == "order_to_customer"
    assert _graph_relationships(workspace) == {
        "order_to_customer": {
            "entities": ["order", "customer"],
            "cardinality": "many_to_one",
            "rollup_safe": {"reverse": ["count_distinct"]},
            "safety": "safe",
        }
    }
    with pytest.raises(SemanticLayerError, match="already related as"):
        project.upsert_relationship(
            from_entity="order", to_entity="customer", columns=["customer_id"], name="other"
        )


def test_a_name_taken_by_another_pair_is_refused(workspace: Path) -> None:
    project = _project(workspace)
    named = project.upsert_relationship(
        from_entity="order", to_entity="customer", columns=["customer_id"], name="orders_store"
    )
    assert named.report["ok"] is True, named.report

    with pytest.raises(SemanticLayerError, match="already relates other entities"):
        project.upsert_relationship(from_entity="order", to_entity="store", columns=["store_id"])


def test_an_entry_from_the_other_side_is_refused(workspace: Path) -> None:
    graph_path = workspace / "shop" / "graph.yml"
    graph = _yaml(graph_path)
    graph["graph"]["relationships"] = {
        "customer_orders": {"entities": ["customer", "order"], "cardinality": "one_to_many"}
    }
    graph_path.write_text(yaml.safe_dump(graph, sort_keys=False), encoding="utf-8")

    with pytest.raises(SemanticLayerError, match="from the customer side"):
        _project(workspace).upsert_relationship(
            from_entity="order", to_entity="customer", columns=["customer_id"]
        )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"cardinality": "many_to_many"}, "needs a bridge model"),
        ({"cardinality": "sometimes"}, "cardinality must be one of"),
        ({"to_columns": ["customer_email"]}, "related through its key"),
        ({"columns": ["customer_id", "store_id"]}, "do not match the width"),
        ({"columns": []}, "foreign-key columns"),
        ({"cardinality": "one_to_many"}, "recorded on the many side"),
        ({"to_entity": "order"}, "cannot reference itself"),
        ({"to_entity": "supplier"}, "is not in this package"),
        ({"safety": "maybe"}, "safety must be one of"),
        ({"allowed_directions": ["sideways"]}, "allowed_directions must be"),
        ({"allowed_directions": []}, "allowed_directions must be"),
        ({"path_preference": 0}, "positive integer"),
    ],
)
def test_requests_the_package_could_not_express_are_refused(
    workspace: Path, arguments: dict[str, Any], message: str
) -> None:
    before = project_revision(workspace / "shop")
    request = {
        "from_entity": "order",
        "to_entity": "customer",
        "columns": ["customer_id"],
        **arguments,
    }

    with pytest.raises(SemanticLayerError, match=message):
        _project(workspace).upsert_relationship(**request)
    assert project_revision(workspace / "shop") == before


def test_a_bridge_false_model_gets_an_explicit_relationship(workspace: Path) -> None:
    orders_path = workspace / "shop" / "models" / "orders.yml"
    orders = _yaml(orders_path)
    orders["model"]["entities"] = {"bridge": False, "order": {}}
    orders_path.write_text(yaml.safe_dump(orders, sort_keys=False), encoding="utf-8")

    mutation = _project(workspace).upsert_relationship(
        from_entity="order", to_entity="customer", columns=["customer_id"]
    )

    assert mutation.report["ok"] is True, mutation.report
    assert mutation.report["relationship"]["override"] is True
    assert _graph_relationships(workspace)["orders_customer"]["entities"] == ["order", "customer"]
    assert "relationship.orders_customer" in _loaded(workspace)


def test_dry_run_apply_and_undo(workspace: Path) -> None:
    project = _project(workspace)
    before = project_revision(workspace / "shop")
    request = {
        "from_entity": "order",
        "to_entity": "customer",
        "columns": ["customer_id"],
        "safety": "safe",
    }

    preview = project.upsert_relationship(**request, dry_run=True)
    assert preview.report["ok"] is True and preview.report["dry_run"] is True
    assert {change["path"] for change in preview.report["changes"]} == {
        "graph.yml",
        "models/orders.yml",
    }
    assert project_revision(workspace / "shop") == before

    applied = project.upsert_relationship(**request)
    assert applied.report["ok"] is True, applied.report
    assert applied.report["revision"] == preview.report["proposed_revision"]
    assert set(applied.changed_files) == {"graph.yml", "models/orders.yml"}

    undone = applied.undo()
    assert undone["ok"] is True, undone
    assert project_revision(workspace / "shop") == before
    assert "customer" not in _orders_entities(workspace)


def test_a_change_that_breaks_a_pinned_route_is_rolled_back(workspace: Path) -> None:
    project = _project(workspace)
    project.upsert_relationship(from_entity="order", to_entity="customer", columns=["customer_id"])
    graph_path = workspace / "shop" / "graph.yml"
    graph = _yaml(graph_path)
    graph["graph"]["path_preferences"] = [
        {
            "source_entity": "order",
            "target_entity": "customer",
            "relationship_path": ["relationship.orders_customer"],
        }
    ]
    graph_path.write_text(yaml.safe_dump(graph, sort_keys=False), encoding="utf-8")
    before = project_revision(workspace / "shop")

    mutation = project.upsert_relationship(
        from_entity="order",
        to_entity="customer",
        columns=["customer_id"],
        allowed_directions=["reverse"],
    )

    assert mutation.report["ok"] is False
    assert "does not connect" in str(mutation.report)
    assert project_revision(workspace / "shop") == before
    assert _graph_relationships(workspace) == {}
