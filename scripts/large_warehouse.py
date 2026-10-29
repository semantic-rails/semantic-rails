"""A 100-relation DuckDB warehouse, its dbt artifacts and a package over part of it.

The shape is what ``dbt build`` on a mid-sized dbt-duckdb project leaves behind: 25 dimension
and 60 fact tables in ``main_marts`` (declared primary keys, and foreign keys from each fact to
three to six dimensions) and 15 staging views in ``main_staging``. Most tables have 10 to 50
columns; three are wide (120 to 180 columns), as real marts often are. ``write_large_dbt_artifacts``
writes the ``manifest.json`` and ``catalog.json`` that project would leave, with model and column
descriptions and the usual tests. ``build_large_workspace`` lays out all of it, and a package
over ``PACKAGE_MARTS`` written through the Architect MCP, as an agent would. Everything is
deterministic: the same call builds the same data, artifacts and package.

``scripts/mcp_context.py`` sizes the Architect MCP's first pages on it, and the paging tests read
every page of it.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import duckdb

from semantic_rails.sql_identifiers import quote_identifier

DIMENSIONS = (
    "customer",
    "product",
    "store",
    "region",
    "supplier",
    "employee",
    "account",
    "campaign",
    "channel",
    "device",
    "carrier",
    "category",
    "brand",
    "promotion",
    "currency",
    "country",
    "department",
    "vendor",
    "plan",
    "partner",
    "team",
    "location",
    "segment",
    "contract",
    "site",
)
DOMAINS = ("sales", "web", "finance", "ops", "support", "people")
EVENTS = (
    "order",
    "return",
    "payment",
    "invoice",
    "session",
    "ticket",
    "shipment",
    "refund",
    "visit",
    "signup",
)
MARTS_SCHEMA = "main_marts"
STAGING_SCHEMA = "main_staging"
DBT_PROJECT = "large_shop"
# Wide tables: real marts often carry a hundred or more columns.
WIDE_DIMENSIONS = {"customer": 120}
WIDE_FACTS = {7: 180, 31: 150}
DIMENSION_ROWS = 40
FACT_ROWS = 300
STATUSES = ("placed", "shipped", "delivered", "returned", "cancelled")
_STATUS_LIST = "[" + ", ".join(f"'{status}'" for status in STATUSES) + "]"


def dimension_table(name: str) -> str:
    return f"dim_{name}s"


def fact_tables() -> list[tuple[int, str, str]]:
    """``(index, table, entity)`` for each fact table, in a fixed order."""
    return [
        (index, f"fct_{domain}_{event}s", f"{domain}_{event}")
        for index, (domain, event) in enumerate(
            (domain, event) for domain in DOMAINS for event in EVENTS
        )
    ]


def fact_dimensions(index: int) -> list[str]:
    """The three to six dimensions fact ``index`` holds keys of."""
    count = 3 + index % 4
    return [DIMENSIONS[(index * 7 + step * 6) % len(DIMENSIONS)] for step in range(count)]


def _dimension_columns(position: int, name: str) -> list[tuple[str, str, str]]:
    """``(column, type, select expression)`` for one dimension table; ``i`` is the row number."""
    width = WIDE_DIMENSIONS.get(name, 6 + (position % 5) * 4)
    columns = [
        (f"{name}_id", "INTEGER", "i"),
        (f"{name}_name", "VARCHAR", f"'{name.title()} ' || i"),
        (f"{name}_type", "VARCHAR", "'type_' || (i % 4)"),
        ("is_active", "BOOLEAN", "i % 5 <> 0"),
        ("created_at", "TIMESTAMP", "TIMESTAMP '2023-01-01' + INTERVAL (i) DAY"),
    ]
    for attribute in range(width - len(columns)):
        columns.append(
            (f"attribute_{attribute + 1:03d}", "VARCHAR", f"'value_' || ((i + {attribute}) % 7)")
        )
    return columns


def _fact_columns(index: int, entity: str) -> list[tuple[str, str, str]]:
    width = WIDE_FACTS.get(index, 10 + (index % 9) * 5)
    columns = [(f"{entity}_id", "INTEGER", "i")]
    for dimension in fact_dimensions(index):
        columns.append((f"{dimension}_id", "INTEGER", f"1 + (i * {index + 3}) % {DIMENSION_ROWS}"))
    columns += [
        ("occurred_at", "TIMESTAMP", "TIMESTAMP '2024-01-01' + INTERVAL (i) HOUR"),
        ("status", "VARCHAR", f"{_STATUS_LIST}[1 + i % {len(STATUSES)}]"),
        ("quantity", "INTEGER", "1 + i % 9"),
        ("gross_amount", "DECIMAL(12,2)", "(i % 97) * 1.25"),
        ("net_amount", "DECIMAL(12,2)", "(i % 97) * 1.10"),
    ]
    for extra in range(width - len(columns)):
        kind = extra % 3
        if kind == 0:
            columns.append(
                (f"metric_{extra + 1:03d}_amount", "DOUBLE", f"(i * {extra + 1}) % 113 / 4.0")
            )
        elif kind == 1:
            columns.append(
                (f"label_{extra + 1:03d}", "VARCHAR", f"'label_' || ((i + {extra}) % 6)")
            )
        else:
            columns.append((f"flag_{extra + 1:03d}", "BOOLEAN", f"(i + {extra}) % 3 = 0"))
    return columns


def _create(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    columns: list[tuple[str, str, str]],
    rows: int,
    *,
    key: str,
    references: tuple[tuple[str, str], ...] = (),
) -> None:
    qualified = f"{MARTS_SCHEMA}.{quote_identifier(table)}"
    definitions = [f"{quote_identifier(name)} {data_type}" for name, data_type, _ in columns]
    definitions.append(f"PRIMARY KEY ({quote_identifier(key)})")
    for column, target in references:
        definitions.append(
            f"FOREIGN KEY ({quote_identifier(column)}) REFERENCES "
            f"{MARTS_SCHEMA}.{quote_identifier(target)} ({quote_identifier(column)})"
        )
    conn.execute(f"CREATE TABLE {qualified} ({', '.join(definitions)})")
    selects = ", ".join(
        f"{expression} AS {quote_identifier(name)}" for name, _, expression in columns
    )
    conn.execute(f"INSERT INTO {qualified} SELECT {selects} FROM range(1, {rows + 1}) AS t(i)")


def large_relations() -> list[str]:
    """Every relation ``build_large_warehouse`` creates, as introspection spells them."""
    return [
        *(f"{MARTS_SCHEMA}.{dimension_table(name)}" for name in DIMENSIONS),
        *(f"{MARTS_SCHEMA}.{table}" for _, table, _ in fact_tables()),
        *(f"{STAGING_SCHEMA}.stg_{table.removeprefix('fct_')}" for _, table, _ in staging_facts()),
    ]


def staging_facts() -> list[tuple[int, str, str]]:
    """The facts a staging view reads: every fourth, the wide ones among them."""
    return fact_tables()[3::4]


def build_large_warehouse(db_path: Path) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute(f"CREATE SCHEMA {MARTS_SCHEMA}")
        conn.execute(f"CREATE SCHEMA {STAGING_SCHEMA}")
        for position, name in enumerate(DIMENSIONS):
            _create(
                conn,
                dimension_table(name),
                _dimension_columns(position, name),
                DIMENSION_ROWS,
                key=f"{name}_id",
            )
        for index, table, entity in fact_tables():
            dimensions = fact_dimensions(index)
            # DuckDB declares a foreign key only to a key of the same schema; every second fact
            # declares its first two, the rest are named like the dimension's key.
            declared = dimensions[:2] if index % 2 == 0 else []
            _create(
                conn,
                table,
                _fact_columns(index, entity),
                FACT_ROWS,
                key=f"{entity}_id",
                references=tuple((f"{name}_id", dimension_table(name)) for name in declared),
            )
        for _, table, _ in staging_facts():
            # A view declares no key: the staged rows, as a staging model selects them.
            view = f"stg_{table.removeprefix('fct_')}"
            conn.execute(
                f"CREATE VIEW {STAGING_SCHEMA}.{quote_identifier(view)} AS SELECT * "
                f"FROM {MARTS_SCHEMA}.{quote_identifier(table)}"
            )
    finally:
        conn.close()
    return db_path


def _node(name: str, schema: str, materialized: str, description: str) -> dict[str, Any]:
    return {
        "unique_id": f"model.{DBT_PROJECT}.{name}",
        "resource_type": "model",
        "name": name,
        "database": "warehouse",
        "schema": schema,
        "alias": name,
        "relation_name": f'"warehouse"."{schema}"."{name}"',
        "description": description,
        "config": {"materialized": materialized},
        "columns": {},
    }


def _test(kind: str, model: str, column: str, **kwargs: Any) -> tuple[str, dict[str, Any]]:
    unique_id = f"test.{DBT_PROJECT}.{kind}_{model}_{column}"
    return unique_id, {
        "unique_id": unique_id,
        "resource_type": "test",
        "name": f"{kind}_{model}_{column}",
        "column_name": column,
        "attached_node": f"model.{DBT_PROJECT}.{model}",
        "test_metadata": {
            "name": kind,
            "namespace": None,
            "kwargs": {"column_name": column, **kwargs},
        },
        "depends_on": {"nodes": [f"model.{DBT_PROJECT}.{model}"]},
    }


def mart_models() -> list[str]:
    """The dbt model names of the marts, in a fixed order."""
    return [dimension_table(name) for name in DIMENSIONS] + [table for _, table, _ in fact_tables()]


def write_large_dbt_artifacts(db_path: Path, target_dir: Path) -> Path:
    """The ``manifest.json`` and ``catalog.json`` a dbt-duckdb project would leave for the
    warehouse ``build_large_warehouse`` built, with descriptions and key, relationship and
    accepted-values tests (catalog read from the warehouse)."""
    nodes: dict[str, Any] = {}
    for name in DIMENSIONS:
        table = dimension_table(name)
        node = _node(table, MARTS_SCHEMA, "table", f"One row per {name}, with its attributes.")
        node["columns"] = {
            f"{name}_id": {"name": f"{name}_id", "description": f"The {name}'s key."},
            f"{name}_name": {"name": f"{name}_name", "description": f"The {name}'s display name."},
        }
        nodes[node["unique_id"]] = node
        for kind in ("unique", "not_null"):
            unique_id, test = _test(kind, table, f"{name}_id")
            nodes[unique_id] = test
    for index, table, entity in fact_tables():
        node = _node(
            table,
            MARTS_SCHEMA,
            "table",
            f"One row per {entity.replace('_', ' ')}, with its amounts and the keys it refers to.",
        )
        node["columns"] = {
            "status": {"name": "status", "description": "Where the record is in its lifecycle."},
            "net_amount": {"name": "net_amount", "description": "Amount after discounts and tax."},
        }
        nodes[node["unique_id"]] = node
        for kind in ("unique", "not_null"):
            unique_id, test = _test(kind, table, f"{entity}_id")
            nodes[unique_id] = test
        unique_id, test = _test("accepted_values", table, "status", values=list(STATUSES))
        nodes[unique_id] = test
        for dimension in fact_dimensions(index):
            unique_id, test = _test(
                "relationships",
                table,
                f"{dimension}_id",
                to=f"ref('{dimension_table(dimension)}')",
                field=f"{dimension}_id",
            )
            nodes[unique_id] = test
    for _, table, _ in staging_facts():
        view = f"stg_{table.removeprefix('fct_')}"
        node = _node(view, STAGING_SCHEMA, "view", "Staged rows, renamed and typed.")
        nodes[node["unique_id"]] = node
    catalog_nodes: dict[str, Any] = {}
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        for unique_id, node in nodes.items():
            if node["resource_type"] != "model":
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


PACKAGE_ID = "large"
# The marts the package models: enough entities that a pair of them has more routes than a
# summary shows, few enough that every check on it runs in about a second.
PACKAGE_MARTS = [dimension_table(name) for name in DIMENSIONS[:10]] + [
    table for _, table, _ in fact_tables()[:24]
]
# Every mart as one package: 85 entities, and a pair of them with 212 routes.
ALL_MARTS = [dimension_table(name) for name in DIMENSIONS] + [
    table for _, table, _ in fact_tables()
]
FACT = f"{MARTS_SCHEMA}.fct_sales_orders"
WIDE_FACT = f"{MARTS_SCHEMA}.fct_sales_refunds"


@dataclass(frozen=True)
class LargeWorkspace:
    """Paths relative to ``root``, the Architect MCP's workspace root."""

    root: Path
    duckdb_path: str
    target_dir: str
    # The package over PACKAGE_MARTS, and a copy of it as create_project left it, before the import.
    package: str
    scaffold: str
    scaffold_revision: str


def call_tool(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """The structured answer of one call to an MCP server's tool."""
    _, structured = cast(tuple[Any, dict[str, Any]], asyncio.run(server.call_tool(name, arguments)))
    return dict(structured)


def build_large_workspace(root: Path, marts: list[str] | None = None) -> LargeWorkspace:
    """The warehouse in the package's ``data/``, the dbt target, and the package over ``marts``
    (``PACKAGE_MARTS`` by default): created and then imported from dbt through the Architect
    MCP."""
    from semantic_rails.architect_mcp import create_architect_mcp_server

    package = root / PACKAGE_ID
    db_path = build_large_warehouse(package / "data" / f"{PACKAGE_ID}.duckdb")
    write_large_dbt_artifacts(db_path, root / "dbt" / "target")
    server = create_architect_mcp_server(workspace_root=root)

    def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = call_tool(server, name, arguments)
        if not result.get("ok"):
            raise RuntimeError(f"{name} failed: {result.get('error') or result.get('status')}")
        return result

    created = call(
        "create_project",
        {
            "package_id": PACKAGE_ID,
            "project_path": PACKAGE_ID,
            "data": "external",
            "default_db": f"data/{PACKAGE_ID}.duckdb",
            "first_entity": "customer",
            "relation": f"{MARTS_SCHEMA}.dim_customers",
            "primary_key": "customer_id",
            "time_column": "created_at",
            "expected_revision": "absent",
            "idempotency_key": "create",
        },
    )
    scaffold = root / "scaffold" / PACKAGE_ID
    shutil.copytree(package, scaffold, ignore=shutil.ignore_patterns("*.duckdb*"))
    call(
        "import_dbt_project",
        {
            "project_path": PACKAGE_ID,
            "target_dir": "dbt/target",
            "select": PACKAGE_MARTS if marts is None else marts,
            "expected_revision": created["revision"],
            "idempotency_key": "import",
        },
    )
    return LargeWorkspace(
        root=root,
        duckdb_path=db_path.relative_to(root).as_posix(),
        target_dir="dbt/target",
        package=PACKAGE_ID,
        scaffold=scaffold.relative_to(root).as_posix(),
        scaffold_revision=str(created["revision"]),
    )
