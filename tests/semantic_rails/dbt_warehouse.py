"""A dbt-shaped DuckDB warehouse, built from SQL at test time (no dbt install).

``build_dbt_warehouse`` runs ``fixtures/dbt_warehouse.sql``, which lays out what
``dbt build`` on a dbt-duckdb project leaves behind: seeds in ``main``, staging
views in ``main_staging`` and mart tables in ``main_marts``.
``write_orders_package`` writes a small strict package over those marts.
"""

from __future__ import annotations

import hashlib
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
