from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

from semantic_rails import architect_transactions
from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.architect_transactions import project_revision
from semantic_rails.cli.scaffold import create_project_report
from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from tests.semantic_rails.dbt_warehouse import build_dbt_warehouse, write_orders_package


@pytest.fixture
def project(tmp_path: Path) -> ArchitectProject:
    """The starter package (events, entity event) plus a customers model."""
    report = create_project_report(
        package_id="shop", workspace_root=str(tmp_path), run_checks=False
    )
    assert report["ok"] is True, report
    project = ArchitectProject(report["project_path"], workspace_root=tmp_path)
    project.upsert_model(
        model_id="customers",
        entity_key="customer",
        relation="raw_customers",
        primary_key=["customer_id"],
    )
    return project


def _relate(project: ArchitectProject, columns: list[str], **arguments) -> dict:
    return project.upsert_relationship(
        from_entity="event", to_entity="customer", columns=columns, **arguments
    ).report


def _event_to_customer(project: ArchitectProject):
    report, config = parse_config_report(PackageReference(source_path=str(project.project_path)))
    assert report["ok"] is True, report
    return next(
        rel
        for rel in config.relationships
        if (rel.source_entity, rel.target_entity) == ("entity.shop_event", "entity.shop_customer")
    )


def _file(project: ArchitectProject, relative: str) -> dict:
    return yaml.safe_load((project.project_path / relative).read_text())


@pytest.mark.parametrize(
    ("columns", "entry"),
    [(["customer_id"], {}), (["buyer_id"], {"expr": "buyer_id"})],
)
def test_foreign_key_becomes_a_many_to_one_relationship(project, columns, entry):
    graph_before = (project.project_path / "graph.yml").read_bytes()

    report = _relate(project, columns)

    assert report["status"] == "upserted"
    assert report["changed_files"] == ["models/core/events.yml"]
    entities = _file(project, "models/core/events.yml")["model"]["entities"]
    assert entities == {"event": {}, "customer": entry}
    assert list(entities) == ["event", "customer"]  # the model's own entity stays first
    assert (project.project_path / "graph.yml").read_bytes() == graph_before
    relationship = _event_to_customer(project)
    assert (relationship.source_columns, relationship.cardinality) == (columns, "N:1")
    assert relationship.id == "relationship.events_customer"


def test_one_to_one_is_recorded_in_graph_relationships_and_can_be_reverted(project):
    _relate(project, ["customer_id"], cardinality="one_to_one")

    entry = _file(project, "graph.yml")["graph"]["relationships"]["events_customer"]
    assert entry == {"entities": ["event", "customer"], "cardinality": "one_to_one"}
    relationship = _event_to_customer(project)
    assert (relationship.id, relationship.cardinality) == ("relationship.events_customer", "1:1")

    _relate(project, ["buyer_id"])

    entry = _file(project, "graph.yml")["graph"]["relationships"]["events_customer"]
    assert entry["cardinality"] == "many_to_one"
    relationship = _event_to_customer(project)
    assert (relationship.source_columns, relationship.cardinality) == (["buyer_id"], "N:1")


def test_composite_key_and_an_existing_entry_whose_via_would_override(project):
    project.upsert_model(
        model_id="visits", entity_key="visit", relation="raw_visits", primary_key=["shop", "day"]
    )
    graph = _file(project, "graph.yml")
    graph["graph"]["relationships"] = {
        "event_visit": {"entities": ["event", "visit"], "via": ["a", "b"], "safety": "safe"}
    }
    project.write_file(relative_path="graph.yml", content=yaml.safe_dump(graph, sort_keys=False))

    project.upsert_relationship(from_entity="event", to_entity="visit", columns=["shop_id", "on"])

    entities = _file(project, "models/core/events.yml")["model"]["entities"]
    assert entities["visit"] == {"expr": ["shop_id", "on"]}
    entry = _file(project, "graph.yml")["graph"]["relationships"]["event_visit"]
    assert entry == {"entities": ["event", "visit"], "safety": "safe", "cardinality": "many_to_one"}
    _, config = parse_config_report(PackageReference(source_path=str(project.project_path)))
    relationship = next(rel for rel in config.relationships if rel.id == "relationship.event_visit")
    assert relationship.source_columns == ["shop_id", "on"]


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"to_entity": "store"}, "OBJECT_NOT_FOUND"),
        ({"columns": ["customer_id", "region"]}, "INVALID_CONFIG"),
        ({"to_entity": "event"}, "INVALID_CONFIG"),
        ({"columns": [" "]}, "INVALID_CONFIG"),
        ({"columns": []}, "INVALID_CONFIG"),
        ({"cardinality": "one_to_many"}, "INVALID_CONFIG"),
        ({"cardinality": "many_to_many"}, "INVALID_CONFIG"),
        ({"cardinality": "one_to_one", "taken": ["event", "event"]}, "INVALID_CONFIG"),
    ],
)
def test_refusals_write_nothing(project, arguments, code):
    if "taken" in arguments:  # the default entry name already relates another pair
        graph = _file(project, "graph.yml")
        graph["graph"]["relationships"] = {"events_customer": {"entities": arguments.pop("taken")}}
        (project.project_path / "graph.yml").write_text(yaml.safe_dump(graph, sort_keys=False))
    revision = project.revision()
    call = {"from_entity": "event", "to_entity": "customer", "columns": ["customer_id"]}

    with pytest.raises(SemanticLayerError) as raised:
        project.upsert_relationship(**{**call, **arguments})

    assert raised.value.code == code
    assert project.revision() == revision


def test_checks_run_after_the_revision_check_and_retries_replay(project):
    stale = project.revision()
    _relate(project, ["customer_id"], expected_revision=stale, idempotency_key="relate-1")

    with pytest.raises(SemanticLayerError) as raised:
        project.upsert_relationship(
            from_entity="event",
            to_entity="store",
            columns=["store_id"],
            expected_revision=stale,
            idempotency_key="relate-2",
        )
    assert raised.value.code == "CONFIG_CONFLICT"

    retry = _relate(project, ["customer_id"], expected_revision=stale, idempotency_key="relate-1")
    assert retry["status"] == "replayed"


@pytest.mark.parametrize(
    "legacy",
    [
        {"joins": {"customer": {"via": "customer_id"}}},
        {"keys": {"primary": ["event_id"], "foreign": {"customer": ["customer_id"]}}},
    ],
)
def test_a_legacy_block_that_would_override_the_columns_is_refused(project, legacy):
    root = project.project_path
    for relative in ("metrics/core.yml", "examples/core.yml", "tests/core.yml"):
        (root / relative).unlink()  # starter metrics clash with auto-published ones
    package = _file(project, "package.yml")
    package["package"]["schema_strict"] = False  # strict packages reject both blocks
    (root / "package.yml").write_text(yaml.safe_dump(package, sort_keys=False))
    events = _file(project, "models/core/events.yml")
    events["model"].update(legacy)
    (root / "models/core/events.yml").write_text(yaml.safe_dump(events, sort_keys=False))
    assert parse_config_report(PackageReference(source_path=str(root)))[0]["ok"] is True

    with pytest.raises(SemanticLayerError, match="legacy"):
        _relate(project, ["buyer_id"])


def test_a_parse_failure_rolls_the_files_back(project, monkeypatch):
    before = {path: path.read_bytes() for path in project.project_path.rglob("*.yml")}
    failed = ({"ok": False, "errors": [{"code": "INVALID_CONFIG", "message": "x"}]}, None)
    monkeypatch.setattr(architect_transactions, "parse_config_report", lambda *_, **__: failed)

    report = _relate(project, ["customer_id"], cardinality="one_to_one")

    assert (report["ok"], report["status"]) == (False, "rolled_back_after_parse_error")
    assert {path: path.read_bytes() for path in project.project_path.rglob("*.yml")} == before


def test_mcp_session_relates_dbt_marts_and_queries_across_them(tmp_path):
    package = write_orders_package(tmp_path, seed={"kind": "external"}, with_customers=False)
    build_dbt_warehouse(package / "data" / "warehouse.duckdb")
    server = create_architect_mcp_server(workspace_root=tmp_path)

    async def session_calls():
        async with create_connected_server_and_client_session(server) as session:

            async def call(name, **arguments):
                result = await session.call_tool(name, {"project_path": "shop", **arguments})
                return dict(result.structuredContent or {})

            model = await call(
                "upsert_model",
                model_id="customers",
                entity_key="customer",
                relation="main_marts.dim_customers",
                primary_key=["customer_id"],
                dimensions={"customer_country": {"label": "Country", "kind": "categorical"}},
                expected_revision=project_revision(package),
                idempotency_key="customers",
            )
            relate = {"from_entity": "order", "to_entity": "customer", "columns": ["customer_id"]}
            revision = model["revision"]
            preview = await call(
                "upsert_relationship",
                **relate,
                expected_revision=revision,
                idempotency_key="preview",
                dry_run=True,
            )
            applied = await call(
                "upsert_relationship", **relate, expected_revision=revision, idempotency_key="apply"
            )
            return preview, applied, await call("validate_project", mode="runtime")

    preview, applied, runtime = asyncio.run(session_calls())

    assert (preview["status"], preview["changed_files"]) == ("preview", ["models/orders.yml"])
    assert (applied["status"], applied["changed_files"]) == ("upserted", ["models/orders.yml"])
    assert runtime["ok"] is True, runtime
    engine = Runtime.from_path(str(package))
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
    country = "dimension.shop_customer_customer_country"
    assert [(row[country], row["orders"]) for row in rows] == [("GB", 3), ("NL", 1), ("US", 4)]
