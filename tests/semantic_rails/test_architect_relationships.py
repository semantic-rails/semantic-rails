from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.cli.scaffold import create_project_report
from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.errors import SemanticLayerError


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
    ],
)
def test_refusals_write_nothing(project, arguments, code):
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


def test_mcp_tool_previews_the_relationship(project):
    server = create_architect_mcp_server(workspace_root=project.workspace_root)
    _, result = asyncio.run(
        server.call_tool(
            "upsert_relationship",
            {
                "project_path": str(project.project_path),
                "from_entity": "event",
                "to_entity": "customer",
                "columns": ["customer_id"],
                "expected_revision": project.revision(),
                "idempotency_key": "preview-1",
                "dry_run": True,
            },
        )
    )

    assert (result["ok"], result["status"]) == (True, "preview")
    assert result["changed_files"] == ["models/core/events.yml"]
    assert result["relationship"]["cardinality"] == "many_to_one"
