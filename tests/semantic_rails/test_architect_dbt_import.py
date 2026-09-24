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
from semantic_rails.dbt_artifacts import dbt_import_models, load_dbt_artifacts
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


@pytest.mark.parametrize("reverse", [False, True])
def test_unattached_relationship_suggests_and_imports_the_child_reference(
    workspace: Path, reverse: bool
) -> None:
    manifest_path = workspace / "dbt" / "target" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    test = manifest["nodes"]["test.shop_dbt.relationships_fct_orders_customer_id"]
    test.pop("attached_node")
    dependencies = ["model.shop_dbt.fct_orders", "model.shop_dbt.dim_customers"]
    test["depends_on"]["nodes"] = list(reversed(dependencies)) if reverse else dependencies
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    server = create_architect_mcp_server(workspace_root=workspace)
    request = {
        "project_path": "shop",
        "target_dir": "dbt/target",
        "select": ["dim_customers", "fct_orders"],
        "expected_revision": project_revision(workspace / "shop"),
        "idempotency_key": f"unattached-{reverse}",
    }

    suggested, preview, applied, replay = _calls(
        server,
        [
            ("suggest_models_from_dbt", {"target_dir": "dbt/target", "select": ["fct_orders"]}),
            ("import_dbt_project", {**request, "dry_run": True}),
            ("import_dbt_project", request),
            ("import_dbt_project", request),
        ],
    )

    assert suggested["ok"] is True and suggested["dbt_warnings"] == []
    assert preview["ok"] is True and preview["dry_run"] is True
    assert applied["ok"] is True and applied["dbt_warnings"] == []
    assert replay["ok"] is True and replay["idempotent_replay"] is True
    assert replay["revision"] == applied["revision"]
    link = suggested["models"][0]["foreign_keys"]
    assert any(
        row["column"] == "customer_id"
        and row["references"]["dbt_unique_id"] == "model.shop_dbt.dim_customers"
        for row in link
    )
    for receipt in (preview, applied):
        assert {"model": "orders", "entity": "customer", "columns": ["customer_id"]} in receipt[
            "references"
        ]
        assert not any(row["model"] == "customers" for row in receipt["references"])
    assert "customer" in _model(workspace / "shop" / "models" / "orders.yml")["entities"]
    assert "customer" in _model(workspace / "shop" / "models" / "dbt" / "customers.yml")["entities"]


def test_unattached_ambiguous_relationship_reports_warning_through_mcp_without_false_fk(
    workspace: Path,
) -> None:
    manifest_path = workspace / "dbt" / "target" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    test_id = "test.shop_dbt.relationships_fct_orders_customer_id"
    test = manifest["nodes"][test_id]
    test["attached_node"] = None
    test["depends_on"]["nodes"] = [
        "model.shop_dbt.fct_orders",
        "model.shop_dbt.dim_customers",
        "model.shop_dbt.dim_stores",
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    server = create_architect_mcp_server(workspace_root=workspace)
    request = {
        "project_path": "shop",
        "target_dir": "dbt/target",
        "select": ["dim_customers", "fct_orders"],
        "expected_revision": project_revision(workspace / "shop"),
        "idempotency_key": "ambiguous-attachment",
    }

    suggested, preview, applied = _calls(
        server,
        [
            ("suggest_models_from_dbt", {"target_dir": "dbt/target", "select": ["fct_orders"]}),
            ("import_dbt_project", {**request, "dry_run": True}),
            ("import_dbt_project", request),
        ],
    )

    assert suggested["ok"] is True
    assert suggested["dbt_warnings"][0]["test"] == test_id
    assert "missing or ambiguous" in suggested["dbt_warnings"][0]["reason"]
    assert not any(
        row.get("column") == "customer_id" for row in suggested["models"][0]["foreign_keys"]
    )
    for receipt in (preview, applied):
        assert receipt["ok"] is True
        assert receipt["dbt_warnings"] == suggested["dbt_warnings"]
        assert not any(
            row["model"] == "orders" and row["entity"] == "customer"
            for row in receipt["references"]
        )
    assert "customer" not in _model(workspace / "shop" / "models" / "orders.yml")["entities"]


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


@pytest.mark.parametrize("artifact", ["manifest.json", "catalog.json"])
@pytest.mark.parametrize("tool", ["suggest_models_from_dbt", "import_dbt_project"])
def test_dbt_target_children_must_resolve_inside_workspace(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, artifact: str, tool: str
) -> None:
    target = workspace / "dbt" / "target"
    child = target / artifact
    outside = workspace.parent / f"{workspace.name}-{artifact}"
    sentinel = "SYNTHETIC_OUTSIDE_ARTIFACT"
    payload = json.loads(child.read_text(encoding="utf-8"))
    payload["synthetic_marker"] = sentinel
    outside.write_text(json.dumps(payload), encoding="utf-8")
    child.unlink()
    child.symlink_to(outside)
    original_read_text = Path.read_text

    def guarded_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if path.resolve() == outside:
            raise AssertionError("outside artifact was read")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    server = create_architect_mcp_server(workspace_root=workspace)
    args: dict[str, Any] = {"target_dir": "dbt/target", "select": ["fct_orders"]}
    if tool == "import_dbt_project":
        args.update(
            project_path="shop",
            expected_revision=project_revision(workspace / "shop"),
            idempotency_key="child-symlink",
            dry_run=True,
        )

    (result,) = _calls(server, [(tool, args)])

    assert result["ok"] is False
    assert "workspace root" in result["error"]["message"]
    assert sentinel not in str(result)


@pytest.mark.parametrize("artifact", ["manifest.json", "catalog.json"])
def test_dbt_target_children_may_link_within_workspace(workspace: Path, artifact: str) -> None:
    target = workspace / "dbt" / "target"
    child = target / artifact
    stored = target / f"stored-{artifact}"
    child.rename(stored)
    child.symlink_to(stored)
    server = create_architect_mcp_server(workspace_root=workspace)

    (result,) = _calls(
        server,
        [("suggest_models_from_dbt", {"target_dir": "dbt/target", "select": ["fct_orders"]})],
    )

    assert result["ok"] is True, result
    assert result["dbt_project"] == "shop_dbt"


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


def _target_model(entity: str, *, model_id: str | None = None) -> dict[str, Any]:
    return _customers(model_id=model_id or f"{entity}_lookup", entity_key=entity)


def _referencing_lines(reference: dict[str, Any]) -> dict[str, Any]:
    return {
        "model_id": "lines",
        "entity_key": "line",
        "relation": "main_marts.fct_order_lines",
        "primary_key": ["order_id", "line_number"],
        "references": [{**reference, "columns": ["buyer_id"], "to_columns": ["customer_id"]}],
    }


@pytest.mark.parametrize("columns", [["buyer_id", "seller_id"], ["seller_id", "buyer_id"]])
def test_two_foreign_keys_to_one_entity_are_reported_without_a_chosen_join(
    workspace: Path, columns: list[str]
) -> None:
    project = ArchitectProject(workspace / "shop", workspace_root=workspace)
    before = project_revision(workspace / "shop")
    lines = _referencing_lines({"entity": "customer"})
    lines["references"] = [
        {"entity": "customer", "columns": [column], "to_columns": ["customer_id"]}
        for column in columns
    ]
    models = [_target_model("customer"), lines]

    preview = project.upsert_models(
        models, expected_revision=before, idempotency_key="dual-fk", dry_run=True
    ).report
    assert preview["ok"] is True, preview
    assert preview["references"] == []
    assert len(preview["skipped_references"]) == 2
    assert {tuple(row["columns"]) for row in preview["skipped_references"]} == {
        ("buyer_id",),
        ("seller_id",),
    }
    assert all("multiple foreign keys" in row["reason"] for row in preview["skipped_references"])
    assert project_revision(workspace / "shop") == before

    applied = project.upsert_models(
        models, expected_revision=before, idempotency_key="dual-fk"
    ).report
    assert applied["ok"] is True, applied
    assert applied["references"] == preview["references"]
    assert applied["skipped_references"] == preview["skipped_references"]
    assert (
        "customer" not in _model(workspace / "shop" / "models" / "core" / "lines.yml")["entities"]
    )
    revision = project_revision(workspace / "shop")
    replayed = project.upsert_models(
        models, expected_revision=before, idempotency_key="dual-fk"
    ).report
    assert replayed["idempotent_replay"] is True
    assert replayed["references"] == applied["references"]
    assert project_revision(workspace / "shop") == revision


@pytest.mark.parametrize("reverse", [False, True])
def test_identical_foreign_keys_to_one_entity_are_recorded_once(
    workspace: Path, reverse: bool
) -> None:
    project = ArchitectProject(workspace / "shop", workspace_root=workspace)
    lines = _referencing_lines({"entity": "customer"})
    references = [
        {"entity": "customer", "columns": ["buyer_id"], "to_columns": ["customer_id"]},
        {
            "relation": "main_marts.dim_customers",
            "columns": ["buyer_id"],
            "to_columns": ["customer_id"],
        },
    ]
    lines["references"] = list(reversed(references)) if reverse else references

    applied = project.upsert_models([_target_model("customer"), lines]).report

    assert applied["ok"] is True, applied
    assert applied["references"] == [
        {"model": "lines", "entity": "customer", "columns": ["buyer_id"]}
    ]
    assert applied["skipped_references"] == []
    assert _model(workspace / "shop" / "models" / "core" / "lines.yml")["entities"]["customer"] == {
        "expr": "buyer_id"
    }


@pytest.mark.parametrize(
    ("existing", "staged", "expected"),
    [
        ([], [], None),
        (["customer"], [], "customer"),
        ([], ["customer"], "customer"),
        (["customer", "client"], [], None),
        ([], ["customer", "client"], None),
        (["customer"], ["client"], None),
    ],
    ids=["zero", "one-existing", "one-staged", "two-existing", "two-staged", "mixed"],
)
def test_relation_reference_requires_one_eligible_entity(
    workspace: Path, existing: list[str], staged: list[str], expected: str | None
) -> None:
    project = ArchitectProject(workspace / "shop", workspace_root=workspace)
    for entity in existing:
        assert project.upsert_model(**_target_model(entity)).report["ok"] is True
    before = project_revision(workspace / "shop")
    models = [_target_model(entity) for entity in staged]
    models.append(_referencing_lines({"relation": "main_marts.dim_customers"}))

    preview = project.upsert_models(models, dry_run=True).report

    assert preview["ok"] is True, preview
    assert project_revision(workspace / "shop") == before
    if expected:
        assert preview["references"] == [
            {"model": "lines", "entity": expected, "columns": ["buyer_id"]}
        ]
        assert preview["skipped_references"] == []
    else:
        assert preview["references"] == []
        assert len(preview["skipped_references"]) == 1
        reason = preview["skipped_references"][0]["reason"]
        assert (
            "multiple eligible entities" in reason
            if existing or staged
            else "not a model" in reason
        )


def test_explicit_entity_and_staged_update_override_relation_ambiguity(workspace: Path) -> None:
    project = ArchitectProject(workspace / "shop", workspace_root=workspace)
    assert project.upsert_model(**_target_model("customer")).report["ok"] is True
    assert project.upsert_model(**_target_model("client")).report["ok"] is True

    explicit = project.upsert_models(
        [_referencing_lines({"entity": "customer", "relation": "main_marts.dim_customers"})],
        dry_run=True,
    ).report
    assert explicit["references"][0]["entity"] == "customer"
    assert explicit["skipped_references"] == []

    update = project.upsert_models(
        [
            _target_model("customer"),
            _referencing_lines(
                {
                    "target_dbt_unique_id": "model.shop_dbt.dim_customers",
                    "relation": "main_marts.dim_customers",
                }
            ),
        ],
        dry_run=True,
    ).report
    assert update["references"] == []
    assert "multiple eligible entities" in update["skipped_references"][0]["reason"]

    same = project.upsert_models(
        [
            _target_model("customer"),
            _referencing_lines({"entity": "customer", "relation": "main_marts.dim_customers"}),
        ],
        dry_run=True,
    ).report
    assert same["references"][0]["entity"] == "customer"


def test_staging_an_existing_model_does_not_count_its_old_entity_twice(workspace: Path) -> None:
    project = ArchitectProject(workspace / "shop", workspace_root=workspace)
    assert project.upsert_model(**_target_model("customer")).report["ok"] is True

    preview = project.upsert_models(
        [
            _target_model("customer"),
            _referencing_lines({"relation": "main_marts.dim_customers"}),
        ],
        dry_run=True,
    ).report

    assert preview["ok"] is True, preview
    assert preview["references"] == [
        {"model": "lines", "entity": "customer", "columns": ["buyer_id"]}
    ]
    assert preview["skipped_references"] == []


def test_selected_dbt_identity_must_match_its_relation(workspace: Path) -> None:
    project = ArchitectProject(workspace / "shop", workspace_root=workspace)
    target = {**_target_model("customer"), "dbt_unique_id": "model.shop_dbt.dim_customers"}
    wrong = _referencing_lines(
        {
            "relation": "main_marts.dim_stores",
            "target_dbt_unique_id": "model.shop_dbt.dim_customers",
        }
    )

    preview = project.upsert_models([target, wrong], dry_run=True).report

    assert preview["ok"] is True, preview
    assert preview["references"] == []
    assert "does not read the referenced relation" in preview["skipped_references"][0]["reason"]


def test_dbt_selected_identity_beats_ambiguous_relation_on_dry_run_and_apply(
    workspace: Path,
) -> None:
    project = ArchitectProject(workspace / "shop", workspace_root=workspace)
    assert project.upsert_model(**_target_model("client")).report["ok"] is True
    dbt = load_dbt_artifacts(workspace / "dbt" / "target")
    items, skipped = dbt_import_models(dbt, ["dim_customers", "fct_orders"])
    assert skipped == []
    customers = next(item for item in items if item["dbt_unique_id"].endswith("dim_customers"))
    orders = next(item for item in items if item["dbt_unique_id"].endswith("fct_orders"))
    assert customers["dbt_unique_id"] == orders["references"][0]["target_dbt_unique_id"]
    before = project_revision(workspace / "shop")

    preview = project.upsert_models(items, dry_run=True).report
    assert preview["ok"] is True and project_revision(workspace / "shop") == before
    assert {row["entity"] for row in preview["references"]} >= {"customer"}
    assert not any("multiple eligible" in row["reason"] for row in preview["skipped_references"])

    applied = project.upsert_models(items).report
    assert applied["ok"] is True, applied
    assert project_revision(workspace / "shop") != before
    assert "customer" in _model(workspace / "shop" / "models" / "orders.yml")["entities"]


def test_dbt_unselected_ambiguous_target_is_reported_on_apply(workspace: Path) -> None:
    project = ArchitectProject(workspace / "shop", workspace_root=workspace)
    for entity in ("customer", "client"):
        assert project.upsert_model(**_target_model(entity)).report["ok"] is True
    dbt = load_dbt_artifacts(workspace / "dbt" / "target")
    items, _ = dbt_import_models(dbt, ["fct_orders"])

    applied = project.upsert_models(items).report

    assert applied["ok"] is True, applied
    assert not any(row["entity"] in ("customer", "client") for row in applied["references"])
    assert any(
        "multiple eligible entities" in row["reason"] for row in applied["skipped_references"]
    )


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


def test_completed_import_replay_preserves_a_later_authored_edit(workspace: Path) -> None:
    server = create_architect_mcp_server(workspace_root=workspace)
    project = ArchitectProject(workspace / "shop", workspace_root=workspace)
    before = project_revision(workspace / "shop")
    request = {
        "project_path": "shop",
        "target_dir": "dbt/target",
        "select": ["dim_customers"],
        "expected_revision": before,
        "idempotency_key": "completed-import",
    }
    (imported,) = _calls(server, [("import_dbt_project", request)])
    assert imported["ok"] is True, imported

    edited = project.upsert_model(
        model_id="customers",
        entity_key="customer",
        relation="main_marts.dim_customers",
        primary_key=["customer_id"],
        description="Edited after dbt import",
        expected_revision=imported["revision"],
        idempotency_key="later-authoring",
    ).report
    assert edited["ok"] is True, edited
    assert edited["revision"] != imported["revision"]

    replayed, stale = _calls(
        server,
        [
            ("import_dbt_project", request),
            ("import_dbt_project", {**request, "idempotency_key": "fresh-stale-key"}),
        ],
    )
    assert replayed["ok"] is True and replayed["idempotent_replay"] is True
    assert replayed["revision"] == imported["revision"]
    assert project_revision(workspace / "shop") == edited["revision"]
    assert (
        _model(workspace / "shop" / "models" / "dbt" / "customers.yml")["description"]
        == "Edited after dbt import"
    )
    assert stale["ok"] is False
    assert stale["error"]["details"]["conflict_kind"] == "stale_revision"


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
