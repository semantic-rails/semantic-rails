"""A dbt-shaped DuckDB warehouse, built from SQL at test time (no dbt install).

``build_dbt_warehouse`` runs ``fixtures/dbt_warehouse.sql``, which lays out what
``dbt build`` on a dbt-duckdb project leaves behind: seeds in ``main``, staging
views in ``main_staging`` and mart tables in ``main_marts``.
``write_orders_package`` writes a small strict package over those marts.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import duckdb
import yaml

FIXTURE_SQL = Path(__file__).parent / "fixtures" / "dbt_warehouse.sql"

# A starter-style placeholder seed: two tiny tables in schema ``main``.
PLACEHOLDER_SEED_SQL = """
CREATE TABLE fct_orders AS
  SELECT 1 AS order_id, 1 AS customer_id, TIMESTAMP '2024-01-01 00:00:00' AS ordered_at,
         'placed' AS status, 10.00 AS order_total;
CREATE TABLE dim_customers AS
  SELECT 1 AS customer_id, 'US' AS customer_country, DATE '2024-01-01' AS signed_up_on;
"""

ORDER_COUNT_QUERY: dict[str, Any] = {
    "version": 1,
    "select": [{"expression": {"measure": "measure.shop.order_count"}, "as": "orders"}],
    "limit": 5,
}


def build_dbt_warehouse(db_path: Path) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute(FIXTURE_SQL.read_text(encoding="utf-8"))
    finally:
        conn.close()
    return db_path


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def write_orders_package(
    root: Path,
    *,
    seed: dict[str, Any] | None = None,
    schema: str = "main_marts",
    with_customers: bool = True,
    package_id: str = "shop",
) -> Path:
    """Write a strict package reading ``<schema>.fct_orders`` (and ``dim_customers``).

    The default seed is the placeholder SQL script above, the way a starter
    scaffold declares one. ``schema=""`` reads unqualified relations instead.
    """
    package_dir = root / package_id
    if seed is None:
        seed = {"kind": "sql_script", "source": "data/seed.sql"}
        (package_dir / "data").mkdir(parents=True, exist_ok=True)
        (package_dir / "data" / "seed.sql").write_text(PLACEHOLDER_SEED_SQL, encoding="utf-8")
    prefix = f"{schema}." if schema else ""
    _dump(
        package_dir / "package.yml",
        {
            "schema_version": 1,
            "package": {
                "id": package_id,
                "namespace": package_id,
                "warehouse": "duckdb",
                "default_db": "data/warehouse.duckdb",
                "seed": seed,
                "schema_strict": True,
            },
        },
    )
    entities: dict[str, Any] = {"order": {"label": "Order", "key": ["order_id"], "model": "orders"}}
    order_entities: dict[str, Any] = {"order": {}}
    if with_customers:
        entities["customer"] = {"label": "Customer", "key": ["customer_id"], "model": "customers"}
        order_entities["customer"] = {}
    _dump(package_dir / "graph.yml", {"graph": {"entities": entities}})
    _dump(
        package_dir / "models" / "orders.yml",
        {
            "model": {
                "id": "orders",
                "label": "Orders",
                "relation": f"{prefix}fct_orders",
                "entities": order_entities,
                "times": {
                    "ordered_at": {
                        "label": "Order time",
                        "column": "ordered_at",
                        "kind": "timestamp",
                        "class": "event_time",
                        "supported_grains": ["day", "month"],
                        "default": True,
                    }
                },
                "dimensions": {"status": {"label": "Order Status", "kind": "categorical"}},
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
        },
    )
    _dump(
        package_dir / "metrics" / "core.yml",
        {
            "metrics": {
                "revenue": {
                    "label": "Revenue",
                    "description": "Total order value.",
                    "kind": "aggregate",
                    "measure": "order_total",
                    "value_type": "currency",
                }
            }
        },
    )
    if with_customers:
        _dump(
            package_dir / "models" / "customers.yml",
            {
                "model": {
                    "id": "customers",
                    "label": "Customers",
                    "relation": f"{prefix}dim_customers",
                    "entities": {"customer": {}},
                    "times": {
                        "signed_up_on": {
                            "label": "Signup date",
                            "column": "signed_up_on",
                            "kind": "date",
                            "class": "event_time",
                            "supported_grains": ["day", "month"],
                            "default": True,
                        }
                    },
                    "dimensions": {
                        "customer_country": {"label": "Customer Country", "kind": "categorical"}
                    },
                    "measures": {
                        "customer_count": {
                            "label": "Customer Count",
                            "kind": "entity_count",
                            "entity_key": "customer_id",
                            "accumulation": {"kind": "event"},
                            "value_type": "count",
                        }
                    },
                }
            },
        )
    return package_dir


# -- dbt artifacts for the same warehouse ----------------------------------------

DBT_PROJECT = "shop_dbt"
_MODEL_SCHEMAS = {"staging": "main_staging", "marts": "main_marts"}
_DESCRIPTIONS = {
    "fct_orders": "One row per order.",
    "fct_order_lines": "One row per order line.",
    "dim_customers": "One row per customer.",
    "order_total": "Order value after discounts.",
    "status": "Where the order is in its lifecycle.",
}


def _model_node(name: str, layer: str, materialized: str, **extra: Any) -> dict[str, Any]:
    return {
        "unique_id": f"model.{DBT_PROJECT}.{name}",
        "resource_type": "model",
        "name": name,
        "database": "warehouse",
        "schema": _MODEL_SCHEMAS[layer],
        "alias": name,
        "relation_name": f'"warehouse"."{_MODEL_SCHEMAS[layer]}"."{name}"',
        "description": _DESCRIPTIONS.get(name, ""),
        "config": {"materialized": materialized},
        "columns": {},
        **extra,
    }


def _test(
    kind: str, model: str, column: str = "", *, namespace: str | None = None, **kwargs: Any
) -> tuple[str, dict[str, Any]]:
    unique_id = f"test.{DBT_PROJECT}.{kind}_{model}_{column or 'rows'}"
    return unique_id, {
        "unique_id": unique_id,
        "resource_type": "test",
        "name": f"{kind}_{model}_{column}",
        "column_name": column or None,
        "attached_node": f"model.{DBT_PROJECT}.{model}",
        "test_metadata": {
            "name": kind,
            "namespace": namespace,
            "kwargs": {**({"column_name": column} if column else {}), **kwargs},
        },
        "depends_on": {"nodes": [f"model.{DBT_PROJECT}.{model}"]},
    }


def write_dbt_artifacts(db_path: Path, target_dir: Path) -> Path:
    """Write the ``manifest.json`` and ``catalog.json`` a dbt-duckdb project would
    leave for the warehouse ``build_dbt_warehouse`` built (catalog read from it)."""
    nodes: dict[str, Any] = {}
    for name in ("raw_customers", "raw_stores", "raw_products", "raw_orders", "raw_order_lines"):
        nodes[f"seed.{DBT_PROJECT}.{name}"] = {
            "unique_id": f"seed.{DBT_PROJECT}.{name}",
            "resource_type": "seed",
            "name": name,
            "database": "warehouse",
            "schema": "main",
            "alias": name,
            "config": {"materialized": "seed"},
            "columns": {},
        }
    for name in ("stg_customers", "stg_stores", "stg_products", "stg_orders", "stg_order_lines"):
        node = _model_node(name, "staging", "view")
        nodes[node["unique_id"]] = node
    dim_customers = _model_node(
        "dim_customers",
        "marts",
        "table",
        config={"materialized": "table", "contract": {"enforced": True}},
        constraints=[{"type": "primary_key", "columns": ["customer_id"]}],
    )
    for name in ("dim_products", "dim_stores", "fct_orders", "fct_order_lines"):
        node = _model_node(name, "marts", "table")
        nodes[node["unique_id"]] = node
    nodes[dim_customers["unique_id"]] = dim_customers
    fct_orders = nodes[f"model.{DBT_PROJECT}.fct_orders"]
    fct_orders["columns"] = {
        "order_total": {"name": "order_total", "description": _DESCRIPTIONS["order_total"]},
        "status": {"name": "status", "description": _DESCRIPTIONS["status"]},
    }
    for unique_id, test in (
        _test("unique", "dim_products", "product_id"),
        _test("not_null", "dim_products", "product_id"),
        _test("unique", "dim_stores", "store_id"),
        _test("not_null", "dim_stores", "store_id"),
        _test("unique", "fct_orders", "order_id"),
        _test("not_null", "fct_orders", "order_id"),
        _test("not_null", "fct_orders", "ordered_at"),
        _test(
            "accepted_values",
            "fct_orders",
            "status",
            values=["placed", "shipped", "delivered", "returned"],
        ),
        _test(
            "relationships",
            "fct_orders",
            "customer_id",
            to="ref('dim_customers')",
            field="customer_id",
        ),
        _test("relationships", "fct_orders", "store_id", to="ref('dim_stores')", field="store_id"),
        _test(
            "unique_combination_of_columns",
            "fct_order_lines",
            namespace="dbt_utils",
            combination_of_columns=["order_id", "line_number"],
        ),
        _test(
            "relationships", "fct_order_lines", "order_id", to="ref('fct_orders')", field="order_id"
        ),
        _test(
            "relationships",
            "fct_order_lines",
            "product_id",
            to="ref('dim_products')",
            field="product_id",
        ),
    ):
        nodes[unique_id] = test
    catalog_nodes: dict[str, Any] = {}
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        for unique_id, node in nodes.items():
            if node["resource_type"] == "test":
                continue
            rows = conn.execute(
                "SELECT column_name, data_type, column_index FROM duckdb_columns() "
                "WHERE schema_name = ? AND table_name = ? ORDER BY column_index",
                [node["schema"], node["alias"]],
            ).fetchall()
            catalog_nodes[unique_id] = {
                "metadata": {
                    "type": "VIEW" if node["config"]["materialized"] == "view" else "BASE TABLE",
                    "schema": node["schema"],
                    "name": node["alias"],
                    "database": "warehouse",
                },
                "columns": {
                    name: {"name": name, "type": data_type, "index": index}
                    for name, data_type, index in rows
                },
            }
    finally:
        conn.close()
    target_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
        "dbt_version": "1.10.0",
        "project_name": DBT_PROJECT,
        "adapter_type": "duckdb",
    }
    (target_dir / "manifest.json").write_text(
        json.dumps({"metadata": metadata, "nodes": nodes, "sources": {}}), encoding="utf-8"
    )
    (target_dir / "catalog.json").write_text(
        json.dumps({"metadata": metadata, "nodes": catalog_nodes, "sources": {}}),
        encoding="utf-8",
    )
    return target_dir
