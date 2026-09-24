"""One project-creation service, shared by the Architect MCP, the CLI and the REPL."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp.client.session import ClientSession
from mcp.shared.context import RequestContext
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ElicitRequestParams, ElicitResult

from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_scaffold import project_scaffold_files
from semantic_rails.architect_service import (
    FirstModel,
    ProjectSpec,
    ProjectWarehouse,
    create_project,
    project_setup_questions,
    project_warehouse_options,
)
from semantic_rails.architect_transactions import project_revision
from semantic_rails.config import load_package_config
from semantic_rails.config_validation import PackageReference
from semantic_rails.errors import SemanticLayerError
from semantic_rails.package_tools import run_examples_report
from tests.semantic_rails.dbt_warehouse import build_dbt_warehouse

ORDERS = FirstModel(
    entity="order",
    relation="main_marts.fct_orders",
    primary_key="order_id",
    time_column="ordered_at",
    amount_column="order_total",
    dimension_column="status",
)
EXTERNAL_SHOP = ProjectSpec(
    package_id="shop",
    description="Orders from the dbt marts.",
    warehouse=ProjectWarehouse(data="external"),
    first_model=ORDERS,
)


def _yaml(path: Path) -> dict[str, Any]:
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def _examples_pass(project: Path) -> dict[str, Any]:
    return run_examples_report(PackageReference(source_path=str(project)))


# -- the service ----------------------------------------------------------------


def test_starter_project_runs_immediately(tmp_path: Path) -> None:
    mutation = create_project("growth", ProjectSpec(package_id="growth"), workspace_root=tmp_path)
    project = tmp_path / "growth"

    assert mutation.report["ok"] is True, mutation.report
    package = _yaml(project / "package.yml")["package"]
    assert package["schema_strict"] is True
    assert package["seed"] == {"kind": "csv_dir_duckdb", "source": "data/growth_csv"}
    assert (project / "data" / "growth_csv" / "raw_events.csv").exists()
    assert "*.duckdb" in (project / ".gitignore").read_text(encoding="utf-8")
    report = _examples_pass(project)
    assert report["ok"] is True, report


def test_external_duckdb_project_reads_the_dbt_marts(tmp_path: Path) -> None:
    mutation = create_project("shop", EXTERNAL_SHOP, workspace_root=tmp_path)
    project = tmp_path / "shop"

    assert mutation.report["ok"] is True, mutation.report
    package = _yaml(project / "package.yml")["package"]
    assert package["seed"] == {"kind": "external"}
    assert package["default_db"] == "data/shop.duckdb"
    assert not (project / "data").exists()  # no seed data: dbt builds the database
    model = _yaml(project / "models" / "core" / "orders.yml")["model"]
    assert model["relation"] == "main_marts.fct_orders"
    assert "Build the database" in mutation.report["next_actions"][0]

    build_dbt_warehouse(project / "data" / "shop.duckdb")
    report = _examples_pass(project)
    assert report["ok"] is True, report


def test_other_warehouses_get_a_connection_block(tmp_path: Path) -> None:
    spec = ProjectSpec(
        package_id="pg_shop",
        warehouse=ProjectWarehouse(
            kind="postgres",
            data="external",
            connection_kind="postgres_native",
            connection_options={"host_env": "PGHOST", "password_env": "PGPASSWORD"},
        ),
        first_model=ORDERS,
    )

    mutation = create_project("pg_shop", spec, workspace_root=tmp_path)

    assert mutation.report["ok"] is True, mutation.report
    package = _yaml(tmp_path / "pg_shop" / "package.yml")["package"]
    assert package["connection"] == {
        "kind": "postgres_native",
        "options": {"host_env": "PGHOST", "password_env": "PGPASSWORD"},
    }
    assert "seed" not in package and "default_db" not in package
    assert load_package_config(str(tmp_path / "pg_shop")).package.warehouse == "postgres"


def test_a_literal_secret_fails_the_parse_gate_and_writes_nothing(tmp_path: Path) -> None:
    spec = ProjectSpec(
        package_id="leaky",
        warehouse=ProjectWarehouse(
            kind="snowflake",
            data="external",
            connection_kind="snowflake_native",
            connection_options={"account_env": "SF_ACCOUNT", "password": "hunter2"},
        ),
        first_model=ORDERS,
    )

    mutation = create_project("leaky", spec, workspace_root=tmp_path)

    assert mutation.report["ok"] is False
    assert not (tmp_path / "leaky" / "package.yml").exists()


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (ProjectSpec(package_id="x", warehouse=ProjectWarehouse(kind="nope")), "unsupported"),
        (
            ProjectSpec(package_id="x", warehouse=ProjectWarehouse(kind="postgres")),
            "DuckDB-only",
        ),
        (
            ProjectSpec(
                package_id="x",
                warehouse=ProjectWarehouse(kind="postgres", data="external"),
            ),
            "connection_kind",
        ),
        (
            ProjectSpec(package_id="x", warehouse=ProjectWarehouse(connection_kind="motherduck")),
            "take no connection",
        ),
        (
            ProjectSpec(
                package_id="x",
                warehouse=ProjectWarehouse(data="external"),
                first_model=FirstModel(relation="../secrets"),
            ),
            "relation must be",
        ),
        (
            ProjectSpec(
                package_id="x",
                warehouse=ProjectWarehouse(data="external"),
                first_model=FirstModel(time_column="ordered at"),
            ),
            "time_column must be",
        ),
    ],
    ids=[
        "unknown-warehouse",
        "starter-outside-duckdb",
        "missing-connection-kind",
        "duckdb-connection",
        "relation-path",
        "column-with-space",
    ],
)
def test_invalid_specs_are_refused_before_anything_is_written(
    tmp_path: Path, spec: ProjectSpec, message: str
) -> None:
    with pytest.raises(SemanticLayerError, match=message):
        create_project("x", spec, workspace_root=tmp_path)
    assert not (tmp_path / "x").exists()


def test_the_directory_must_match_the_package_id_and_stay_in_the_workspace(
    tmp_path: Path,
) -> None:
    with pytest.raises(SemanticLayerError, match="must match the project directory"):
        create_project("elsewhere", ProjectSpec(package_id="shop"), workspace_root=tmp_path)
    with pytest.raises(SemanticLayerError, match="inside its configured workspace root"):
        create_project(
            tmp_path.parent / "shop", ProjectSpec(package_id="shop"), workspace_root=tmp_path
        )


def test_dry_run_previews_without_writing(tmp_path: Path) -> None:
    mutation = create_project("shop", EXTERNAL_SHOP, workspace_root=tmp_path, dry_run=True)

    assert mutation.report["ok"] is True
    assert mutation.report["dry_run"] is True
    assert {change["path"] for change in mutation.report["changes"]} == set(
        project_scaffold_files(EXTERNAL_SHOP)
    )
    assert not (tmp_path / "shop").exists()


def test_create_is_idempotent_and_guarded_by_revision(tmp_path: Path) -> None:
    first = create_project("shop", EXTERNAL_SHOP, workspace_root=tmp_path, idempotency_key="k1")
    replay = create_project("shop", EXTERNAL_SHOP, workspace_root=tmp_path, idempotency_key="k1")

    assert first.report["ok"] is True
    assert replay.report["status"] == "replayed"
    with pytest.raises(SemanticLayerError) as excinfo:
        create_project("shop", EXTERNAL_SHOP, workspace_root=tmp_path, idempotency_key="k2")
    assert excinfo.value.code == "CONFIG_CONFLICT"


def test_overwrite_needs_the_current_revision_and_the_flag(tmp_path: Path) -> None:
    create_project("shop", EXTERNAL_SHOP, workspace_root=tmp_path)
    current = project_revision(tmp_path / "shop")

    with pytest.raises(SemanticLayerError, match="overwrite"):
        create_project("shop", EXTERNAL_SHOP, workspace_root=tmp_path, expected_revision=current)
    replaced = create_project(
        "shop",
        replace(EXTERNAL_SHOP, description="Orders, rewritten."),
        workspace_root=tmp_path,
        expected_revision=current,
        overwrite=True,
    )
    assert replaced.report["ok"] is True, replaced.report
    assert _yaml(tmp_path / "shop" / "package.yml")["package"]["description"] == (
        "Orders, rewritten."
    )


def test_overwrite_retires_only_the_generated_first_model_and_undo_restores_it(
    tmp_path: Path,
) -> None:
    create_project("shop", EXTERNAL_SHOP, workspace_root=tmp_path)
    project = tmp_path / "shop"
    old_model = project / "models" / "core" / "orders.yml"
    original_model = old_model.read_bytes()
    notes = project / "notes.md"
    notes.write_text("Keep my notes.\n", encoding="utf-8")
    warehouse = project / "data" / "shop.duckdb"
    warehouse.parent.mkdir(exist_ok=True)
    warehouse.write_bytes(b"keep external warehouse")
    revision = project_revision(project)
    customer = replace(
        EXTERNAL_SHOP,
        first_model=FirstModel(
            entity="customer",
            relation="main_marts.dim_customers",
            primary_key="customer_id",
            time_column="created_at",
        ),
    )

    preview = create_project(
        "shop",
        customer,
        workspace_root=tmp_path,
        expected_revision=revision,
        overwrite=True,
        dry_run=True,
    )
    assert preview.report["ok"] is True, preview.report
    assert {row["path"]: row["operation"] for row in preview.report["changes"]}[
        "models/core/orders.yml"
    ] == "delete"
    assert old_model.read_bytes() == original_model
    assert not (project / "models" / "core" / "customers.yml").exists()

    mutation = create_project(
        "shop", customer, workspace_root=tmp_path, expected_revision=revision, overwrite=True
    )
    assert mutation.report["ok"] is True, mutation.report
    assert not old_model.exists()
    assert (project / "models" / "core" / "customers.yml").exists()
    assert notes.read_text(encoding="utf-8") == "Keep my notes.\n"
    assert warehouse.read_bytes() == b"keep external warehouse"

    assert mutation.undo()["ok"] is True
    assert old_model.read_bytes() == original_model
    assert not (project / "models" / "core" / "customers.yml").exists()
    assert notes.read_text(encoding="utf-8") == "Keep my notes.\n"
    assert warehouse.read_bytes() == b"keep external warehouse"


def test_overwrite_refuses_to_delete_a_modified_first_model(tmp_path: Path) -> None:
    create_project("shop", EXTERNAL_SHOP, workspace_root=tmp_path)
    project = tmp_path / "shop"
    old_model = project / "models" / "core" / "orders.yml"
    old_model.write_bytes(old_model.read_bytes() + b"# authored change\n")
    current = project_revision(project)
    customer = replace(EXTERNAL_SHOP, first_model=replace(ORDERS, entity="customer"))

    with pytest.raises(SemanticLayerError, match="modified") as exc:
        create_project(
            "shop", customer, workspace_root=tmp_path, expected_revision=current, overwrite=True
        )
    assert exc.value.code == "INVALID_CONFIG"
    assert old_model.read_bytes().endswith(b"# authored change\n")
    assert not (project / "models" / "core" / "customers.yml").exists()


@pytest.mark.parametrize("dry_run", [True, False])
def test_same_path_entity_overwrite_preserves_modified_model(tmp_path: Path, dry_run: bool) -> None:
    create_project("shop", EXTERNAL_SHOP, workspace_root=tmp_path)
    project = tmp_path / "shop"
    graph = project / "graph.yml"
    original_graph = graph.read_bytes()
    model = project / "models" / "core" / "orders.yml"
    model.write_bytes(model.read_bytes() + b"# authored change\n")
    authored_model = model.read_bytes()
    renamed = replace(EXTERNAL_SHOP, first_model=replace(ORDERS, entity="orders"))

    with pytest.raises(SemanticLayerError, match="modified"):
        create_project(
            "shop",
            renamed,
            workspace_root=tmp_path,
            expected_revision=project_revision(project),
            overwrite=True,
            dry_run=dry_run,
        )
    assert graph.read_bytes() == original_graph
    assert model.read_bytes() == authored_model


def test_same_path_entity_overwrite_replaces_unchanged_scaffold_model(tmp_path: Path) -> None:
    create_project("shop", EXTERNAL_SHOP, workspace_root=tmp_path)
    project = tmp_path / "shop"
    renamed = replace(EXTERNAL_SHOP, first_model=replace(ORDERS, entity="orders"))

    mutation = create_project(
        "shop",
        renamed,
        workspace_root=tmp_path,
        expected_revision=project_revision(project),
        overwrite=True,
    )
    assert mutation.report["ok"] is True, mutation.report
    assert "orders_count" in _yaml(project / "models" / "core" / "orders.yml")["model"]["measures"]


def test_starter_overwrite_retires_old_model_but_keeps_seed(tmp_path: Path) -> None:
    original = ProjectSpec(package_id="growth")
    create_project("growth", original, workspace_root=tmp_path)
    project = tmp_path / "growth"
    seed = project / "data" / "growth_csv" / "raw_events.csv"
    seed_bytes = seed.read_bytes()
    updated = replace(original, first_model=replace(original.first_model, entity="customer"))

    mutation = create_project(
        "growth",
        updated,
        workspace_root=tmp_path,
        expected_revision=project_revision(project),
        overwrite=True,
    )
    assert mutation.report["ok"] is True, mutation.report
    assert not (project / "models" / "core" / "events.yml").exists()
    assert (project / "models" / "core" / "customers.yml").exists()
    assert seed.read_bytes() == seed_bytes


def test_undo_removes_a_created_project(tmp_path: Path) -> None:
    mutation = create_project("shop", EXTERNAL_SHOP, workspace_root=tmp_path)

    undone = mutation.undo()

    assert undone["ok"] is True, undone
    assert not (tmp_path / "shop" / "package.yml").exists()


def test_setup_questions_and_warehouse_options_come_from_the_registry() -> None:
    options = {row["kind"]: row for row in project_warehouse_options()}
    questions = {row["id"]: row for row in project_setup_questions()}

    assert options["duckdb"]["data_modes"] == ["starter", "external"]
    assert options["postgres"]["data_modes"] == ["external"]
    assert options["postgres"]["connection_kinds"] == ["postgres_native"]
    assert "duckdb" in questions["warehouse"]["choices"]
    assert questions["data"]["when"] == {"warehouse": ["duckdb"]}
    assert questions["connection_kind"]["choices_by_warehouse"]["snowflake"] == [
        "snowflake_cli",
        "snowflake_native",
    ]
    assert "host_env" in questions["connection_options"]["options_by_warehouse"]["postgres"]


# -- the MCP tool, end to end ------------------------------------------------------


def _session_call(server: Any, calls: list[tuple[str, dict[str, Any]]], **kwargs: Any) -> list[Any]:
    async def run() -> list[Any]:
        results = []
        async with create_connected_server_and_client_session(server, **kwargs) as session:
            for name, arguments in calls:
                result = await session.call_tool(name, arguments)
                assert not result.isError, result
                results.append(result.structuredContent)
        return results

    return asyncio.run(run())


def test_mcp_session_creates_a_dbt_backed_project_and_validates_it(tmp_path: Path) -> None:
    server = create_architect_mcp_server(workspace_root=tmp_path)
    arguments = {
        "package_id": "shop",
        "project_path": "shop",
        "expected_revision": "absent",
        "warehouse": "duckdb",
        "data": "external",
        "first_entity": "order",
        "relation": "main_marts.fct_orders",
        "primary_key": "order_id",
        "time_column": "ordered_at",
        "amount_column": "order_total",
        "dimension_column": "status",
    }

    preview, created = _session_call(
        server,
        [
            ("create_project", {**arguments, "idempotency_key": "preview", "dry_run": True}),
            ("create_project", {**arguments, "idempotency_key": "create"}),
        ],
    )
    assert preview["ok"] is True and preview["dry_run"] is True
    assert created["ok"] is True, created
    build_dbt_warehouse(tmp_path / "shop" / "data" / "shop.duckdb")

    (runtime,) = _session_call(
        server, [("validate_project", {"project_path": "shop", "mode": "runtime"})]
    )
    assert runtime["ok"] is True, runtime


def test_mcp_setup_dialog_elicits_the_warehouse(tmp_path: Path) -> None:
    server = create_architect_mcp_server(workspace_root=tmp_path)

    async def answer(
        context: RequestContext[ClientSession, Any], params: ElicitRequestParams
    ) -> ElicitResult:
        return ElicitResult(
            action="accept",
            content={
                "package_id": "shop",
                "warehouse": "duckdb",
                "data": "external",
                "relation": "main_marts.fct_orders",
            },
        )

    (dialog,) = _session_call(
        server,
        [("setup_project_dialog", {"package_id": "shop", "interactive": True})],
        elicitation_callback=answer,
    )

    assert dialog["mode"] == "elicitation"
    assert dialog["draft_arguments"]["data"] == "external"
    assert dialog["draft_arguments"]["relation"] == "main_marts.fct_orders"


def test_mcp_dialog_to_create_postgres_round_trip(tmp_path: Path) -> None:
    server = create_architect_mcp_server(workspace_root=tmp_path)

    async def answer(
        context: RequestContext[ClientSession, Any], params: ElicitRequestParams
    ) -> ElicitResult:
        return ElicitResult(
            action="accept",
            content={
                "package_id": "pg_shop",
                "warehouse": "postgres",
                "connection_kind": "postgres_native",
                "connection_options": '{"host_env":"PGHOST","password_env":"PGPASSWORD"}',
                "first_entity": "order",
                "relation": "main_marts.fct_orders",
                "primary_key": "order_id",
                "time_column": "ordered_at",
            },
        )

    (dialog,) = _session_call(
        server,
        [("setup_project_dialog", {"package_id": "pg_shop", "interactive": True})],
        elicitation_callback=answer,
    )
    assert dialog["ok"] is True, dialog
    draft = dialog["draft_arguments"]
    assert draft["data"] == "external"
    assert draft["connection_options"] == {"host_env": "PGHOST", "password_env": "PGPASSWORD"}
    (created,) = _session_call(
        server,
        [("create_project", {**draft, "idempotency_key": "pg-create", "dry_run": False})],
    )
    assert created["ok"] is True, created
    package = _yaml(tmp_path / "configs" / "semantic_rails" / "pg_shop" / "package.yml")
    assert package["package"]["connection"]["options"] == draft["connection_options"]


def test_mcp_dialog_does_not_claim_missing_connection_details_are_ready(tmp_path: Path) -> None:
    server = create_architect_mcp_server(workspace_root=tmp_path)

    async def answer(
        context: RequestContext[ClientSession, Any], params: ElicitRequestParams
    ) -> ElicitResult:
        return ElicitResult(
            action="accept",
            content={
                "package_id": "pg_shop",
                "warehouse": "postgres",
                "connection_kind": "postgres_native",
            },
        )

    (dialog,) = _session_call(
        server,
        [("setup_project_dialog", {"package_id": "pg_shop", "interactive": True})],
        elicitation_callback=answer,
    )
    assert dialog["ok"] is False
    assert dialog["status"] == "needs_connection_details"
    assert dialog["draft_arguments"]["data"] == "external"


def test_mcp_dialog_requires_named_snowflake_cli_connection(tmp_path: Path) -> None:
    server = create_architect_mcp_server(workspace_root=tmp_path)

    async def answer(
        context: RequestContext[ClientSession, Any], params: ElicitRequestParams
    ) -> ElicitResult:
        return ElicitResult(
            action="accept",
            content={
                "package_id": "snow_shop",
                "warehouse": "snowflake",
                "connection_kind": "snowflake_cli",
                "connection_options": '{"database":"ANALYTICS"}',
            },
        )

    (dialog,) = _session_call(
        server,
        [("setup_project_dialog", {"package_id": "snow_shop", "interactive": True})],
        elicitation_callback=answer,
    )
    assert dialog["ok"] is False
    assert dialog["status"] == "needs_connection_details"
    assert "connection_name" in dialog["required_answers"]


@pytest.mark.parametrize("connection_options", ["{}", '{"host_env":"DATABRICKS_HOST"}'])
def test_mcp_dialog_requires_databricks_connection_options(
    tmp_path: Path, connection_options: str
) -> None:
    server = create_architect_mcp_server(workspace_root=tmp_path)

    async def incomplete(
        context: RequestContext[ClientSession, Any], params: ElicitRequestParams
    ) -> ElicitResult:
        return ElicitResult(
            action="accept",
            content={
                "package_id": "lake_shop",
                "warehouse": "databricks",
                "connection_kind": "databricks_native",
                "connection_name": "my_profile",
                "connection_options": connection_options,
            },
        )

    (dialog,) = _session_call(
        server,
        [("setup_project_dialog", {"package_id": "lake_shop", "interactive": True})],
        elicitation_callback=incomplete,
    )
    assert dialog["ok"] is False
    assert dialog["status"] == "needs_connection_details"
    if connection_options == "{}":
        assert "host or host_env" in dialog["required_answers"]
    assert "http_path or http_path_env" in dialog["required_answers"]
    assert "token_env or token_file" in dialog["required_answers"]

    async def complete(
        context: RequestContext[ClientSession, Any], params: ElicitRequestParams
    ) -> ElicitResult:
        return ElicitResult(
            action="accept",
            content={
                "package_id": "lake_shop",
                "warehouse": "databricks",
                "connection_kind": "databricks_native",
                "connection_options": (
                    '{"host_env":"DATABRICKS_HOST",'
                    '"http_path_env":"DATABRICKS_HTTP_PATH",'
                    '"token_env":"DATABRICKS_TOKEN"}'
                ),
            },
        )

    (ready,) = _session_call(
        server,
        [("setup_project_dialog", {"package_id": "lake_shop", "interactive": True})],
        elicitation_callback=complete,
    )
    assert ready["ok"] is True, ready
    assert ready["draft_arguments"]["data"] == "external"
    (created,) = _session_call(
        server,
        [
            (
                "create_project",
                {**ready["draft_arguments"], "idempotency_key": "lake-create", "dry_run": False},
            )
        ],
    )
    assert created["ok"] is True, created
    package = _yaml(tmp_path / "configs" / "semantic_rails" / "lake_shop" / "package.yml")
    assert (
        package["package"]["connection"]["options"]
        == ready["draft_arguments"]["connection_options"]
    )


def test_create_project_where_dbt_already_built_the_warehouse(tmp_path: Path) -> None:
    """B2: the package directory already holds the database dbt wrote there; it
    has no authored files yet, so its revision is still absent."""
    build_dbt_warehouse(tmp_path / "shop" / "data" / "shop.duckdb")
    assert project_revision(tmp_path / "shop") == "absent"

    mutation = create_project("shop", EXTERNAL_SHOP, workspace_root=tmp_path)

    assert mutation.report["ok"] is True, mutation.report
    assert _examples_pass(tmp_path / "shop")["ok"] is True


def test_external_projects_carry_no_placeholders(tmp_path: Path) -> None:
    """B2: nothing the warehouse does not have (no event_type, no amount)."""
    spec = ProjectSpec(
        package_id="shop",
        warehouse=ProjectWarehouse(data="external"),
        first_model=FirstModel(
            entity="order",
            relation="main_marts.fct_orders",
            primary_key="order_id",
            time_column="ordered_at",
        ),
    )

    create_project("shop", spec, workspace_root=tmp_path)

    model = _yaml(tmp_path / "shop" / "models" / "core" / "orders.yml")["model"]
    assert "dimensions" not in model and "topics" not in model
    assert set(model["measures"]) == {"order_count"}
    assert set(_yaml(tmp_path / "shop" / "metrics" / "core.yml")["metrics"]) == {"order_count"}
