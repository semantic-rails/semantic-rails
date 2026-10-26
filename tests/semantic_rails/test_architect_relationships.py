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
    project.write_files([{"path": "graph.yml", "content": yaml.safe_dump(graph, sort_keys=False)}])

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


def _write_graph_relationships(project: ArchitectProject, relationships: dict) -> dict:
    """Write the authored graph with any explicitly recorded decisions."""
    graph = _file(project, "graph.yml")
    graph["graph"]["relationships"] = relationships
    project.write_files([{"path": "graph.yml", "content": yaml.safe_dump(graph, sort_keys=False)}])
    return _file(project, "graph.yml")


ROLE_LAYOUTS = {
    "two_entries": (
        False,
        {
            "event_buyer": {"entities": ["event", "customer"], "via": ["buyer_id"]},
            "event_seller": {"entities": ["event", "customer"], "via": ["seller_id"]},
        },
    ),
    # The model's `entities` foreign key is one role and the entry's `via` another.
    "inferred_key_and_entry_with_via": (
        True,
        {"event_seller": {"entities": ["event", "customer"], "via": ["seller_id"]}},
    ),
    "entry_from_the_other_side": (
        False,
        {"customer_events": {"entities": ["customer", "event"], "via": ["customer_id"]}},
    ),
    "entries_from_both_sides": (
        False,
        {
            "event_buyer": {"entities": ["event", "customer"], "via": ["buyer_id"]},
            "customer_events": {"entities": ["customer", "event"], "via": ["customer_id"]},
        },
    ),
}


@pytest.mark.parametrize("layout", ROLE_LAYOUTS)
def test_a_pair_that_may_have_several_roles_is_refused_not_rewritten(project, layout):
    """One foreign-key write can change one role, so a pair with another role (a second entry,
    an entry from the other side, or an entry whose `via` differs from the model's foreign key)
    is refused with nothing written, rather than rewriting or merging a role away."""
    with_foreign_key, relationships = ROLE_LAYOUTS[layout]
    if with_foreign_key:
        _relate(project, ["buyer_id"])
        for source, target in [("event", "customer"), ("customer", "event")]:
            project.record_route_decision(
                source_entity=f"entity.shop_{source}",
                target_entity=f"entity.shop_{target}",
                relationship_path=["relationship.events_customer"],
            )
    graph = _write_graph_relationships(project, relationships)
    model = (project.project_path / "models/core/events.yml").read_bytes()
    revision = project.revision()

    with pytest.raises(SemanticLayerError) as raised:
        _relate(project, ["buyer_id"])

    assert raised.value.code == "INVALID_CONFIG"
    assert sorted(raised.value.details["relationships"]) == sorted(relationships)
    assert project.revision() == revision
    assert _file(project, "graph.yml") == graph
    assert (project.project_path / "models/core/events.yml").read_bytes() == model


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


# -- row-grain models and second roles ------------------------------------------------------


def _relationships(project: ArchitectProject, source: str, target: str) -> dict[str, list[str]]:
    """Each relationship from ``source`` to ``target`` (entity ids), by id: its source columns."""
    report, config = parse_config_report(PackageReference(source_path=str(project.project_path)))
    assert report["ok"] is True, report
    return {
        rel.id: list(rel.source_columns)
        for rel in config.relationships
        if (rel.source_entity, rel.target_entity) == (source, target)
    }


def _row_grain_scans(project: ArchitectProject, **arguments) -> dict:
    """A scan log with no key: a row-grain model, whose graph entity is named after it."""
    return project.upsert_model(
        model_id="scans", entity_key="scans", relation="raw_scans", primary_key=[], **arguments
    ).report


@pytest.mark.parametrize(
    ("source", "target"), [("scans", "customer"), ("entity.shop_scans", "entity.shop_customer")]
)
def test_a_row_grain_model_relates_from_its_side_by_model_or_entity_id(project, source, target):
    _row_grain_scans(project)

    report = project.upsert_relationship(
        from_entity=source, to_entity=target, columns=["buyer_id"]
    ).report

    assert report["status"] == "upserted"
    assert report["changed_files"] == ["models/core/scans.yml"]
    model = _file(project, "models/core/scans.yml")["model"]
    assert model["entities"] == {"customer": {"expr": "buyer_id"}}  # its parents only
    assert _relationships(project, "entity.shop_scans", "entity.shop_customer") == {
        "relationship.scans_customer": ["buyer_id"]
    }


def test_a_relationship_into_a_row_grain_model_says_to_write_it_from_its_side(project):
    _row_grain_scans(project)
    revision = project.revision()

    with pytest.raises(SemanticLayerError) as raised:
        project.upsert_relationship(from_entity="customer", to_entity="scans", columns=["scan"])

    assert raised.value.code == "INVALID_CONFIG"
    message = str(raised.value)
    assert "row grain" in message and "from_entity='scans'" in message
    assert project.revision() == revision


def test_upsert_model_writes_a_models_parent_entities(project):
    report = _row_grain_scans(project, entities={"customer": {"expr": "buyer_id"}, "event": {}})

    assert report["ok"] is True, report
    model = _file(project, "models/core/scans.yml")["model"]
    assert model["entities"] == {"customer": {"expr": "buyer_id"}, "event": {}}
    assert _relationships(project, "entity.shop_scans", "entity.shop_customer") == {
        "relationship.scans_customer": ["buyer_id"]
    }

    project.upsert_model(
        model_id="orders",
        entity_key="order",
        relation="raw_orders",
        primary_key=["order_id"],
        entities={"customer": {}},
    )
    assert _file(project, "models/core/orders.yml")["model"]["entities"] == {
        "order": {},
        "customer": {},
    }


def test_the_mcp_tools_take_parent_entities_and_a_role_name(tmp_path):
    server = create_architect_mcp_server(workspace_root=tmp_path)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert "entities" in tools["upsert_model"].inputSchema["properties"]
    assert "name" in tools["upsert_relationship"].inputSchema["properties"]


@pytest.mark.parametrize("source", ["event", "scans"])
def test_a_second_role_is_a_named_relationship_on_its_own_columns(project, source):
    if source == "scans":
        _row_grain_scans(project)
    project.upsert_relationship(from_entity=source, to_entity="customer", columns=["buyer_id"])
    model_path = f"models/core/{'events' if source == 'event' else 'scans'}.yml"
    model = _file(project, model_path)

    report = project.upsert_relationship(
        from_entity=source,
        to_entity="customer",
        columns=["seller_id"],
        name=f"{source}_seller",
        keep_existing_routes=True,
    ).report

    assert report["ok"] is True, report
    assert _file(project, model_path) == model  # the first role is untouched
    entry = _file(project, "graph.yml")["graph"]["relationships"][f"{source}_seller"]
    assert entry == {
        "entities": [source, "customer"],
        "cardinality": "many_to_one",
        "via": ["seller_id"],
        "target": ["customer_id"],
    }
    roles = _relationships(project, f"entity.shop_{source}", "entity.shop_customer")
    assert sorted(roles.values()) == [["buyer_id"], ["seller_id"]]

    # The same name updates that role; a pair with two roles needs the name.
    project.upsert_relationship(
        from_entity=source, to_entity="customer", columns=["agent_id"], name=f"{source}_seller"
    )
    entry = _file(project, "graph.yml")["graph"]["relationships"][f"{source}_seller"]
    assert entry["via"] == ["agent_id"]
    with pytest.raises(SemanticLayerError) as raised:
        project.upsert_relationship(from_entity=source, to_entity="customer", columns=["x_id"])
    assert "name" in str(raised.value)


def test_a_role_name_that_relates_another_pair_is_refused(project):
    project.upsert_model(
        model_id="stores", entity_key="store", relation="raw_stores", primary_key=["store_id"]
    )
    project.upsert_relationship(
        from_entity="event", to_entity="store", columns=["store_id"], name="event_party"
    )
    revision = project.revision()

    with pytest.raises(SemanticLayerError) as raised:
        project.upsert_relationship(
            from_entity="event", to_entity="customer", columns=["buyer_id"], name="event_party"
        )

    assert raised.value.code == "INVALID_CONFIG"
    assert project.revision() == revision


def test_a_model_keyed_by_keys_primary_is_not_called_row_grain(project):
    """The loader keys a model by its keys.primary even with no graph entity; Architect
    must not call it row grain."""
    model = {
        "model": {
            "id": "stores",
            "relation": "raw_stores",
            "description": "One row per store.",
            "keys": {"primary": ["store_id"]},
        }
    }
    project.write_files(
        [{"path": "models/core/stores.yml", "content": yaml.safe_dump(model, sort_keys=False)}]
    )
    revision = project.revision()

    for source, target in (("event", "stores"), ("stores", "customer")):
        with pytest.raises(SemanticLayerError) as raised:
            project.upsert_relationship(from_entity=source, to_entity=target, columns=["x_id"])
        assert "row grain" not in str(raised.value)
    assert project.revision() == revision


@pytest.mark.parametrize(
    ("columns", "name", "taken"),
    [
        (["buyer_id"], "event_buyer2", "events_customer"),  # the model's foreign key's columns
        (["seller_id"], "event_agent", "event_seller"),  # another role's columns
        (["agent_id"], "events_customer", "events_customer"),  # the foreign key's own name
    ],
)
def test_a_role_on_columns_or_a_name_another_role_holds_is_refused(project, columns, name, taken):
    project.upsert_relationship(from_entity="event", to_entity="customer", columns=["buyer_id"])
    project.upsert_relationship(
        from_entity="event",
        to_entity="customer",
        columns=["seller_id"],
        name="event_seller",
        keep_existing_routes=True,
    )
    revision = project.revision()

    with pytest.raises(SemanticLayerError) as raised:
        project.upsert_relationship(
            from_entity="event",
            to_entity="customer",
            columns=columns,
            name=name,
            keep_existing_routes=True,
        )

    assert raised.value.code == "INVALID_CONFIG"
    assert f"relationship.{taken}" in str(raised.value)
    assert raised.value.details["relationship"] == taken
    assert project.revision() == revision
