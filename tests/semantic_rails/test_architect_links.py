"""Architect proposes relationships from a table to other tables' keys, from their values.

``suggest_links`` pairs a column with another table's key (a column named like the key, or the
columns the author names) and counts its values found among the keys: by distinct value (the
``coverage`` the stated ``threshold`` reads) and by row, with a few values not found. Pairs at or
over the threshold are proposed; pairs under it are listed with the same evidence, for the author
to decide; a target key that repeats is no lookup. Nothing is written: the drafts apply with
``dry_run``, then ``expected_revision``, and nothing checks data at query time.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

from semantic_rails.architect_introspection import open_duckdb, suggest_links
from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime

SEED = """
CREATE TABLE fac_buildings (fac_building_key VARCHAR PRIMARY KEY, height INTEGER);
INSERT INTO fac_buildings VALUES ('b1', 30), ('b2', 12), ('b3', 8);
-- One reporting row per building: the same keys under another name.
CREATE TABLE buildings (building_key VARCHAR, building_name VARCHAR);
INSERT INTO buildings VALUES ('b1', 'North Hall'), ('b2', 'South Hall'), ('b3', 'Annex');
CREATE TABLE subjects (subject_id INTEGER PRIMARY KEY, title VARCHAR);
INSERT INTO subjects SELECT i, 'Subject ' || i FROM range(1, 24) AS t(i);
-- 25 subjects used, 23 of them in the catalogue (92%), and one row with none.
CREATE TABLE details (detail_id INTEGER, subject_id INTEGER, fee INTEGER);
INSERT INTO details SELECT i, i, 10 FROM range(1, 26) AS t(i);
INSERT INTO details VALUES (26, 1, 10), (27, NULL, 10);
CREATE TABLE rooms (room_key VARCHAR PRIMARY KEY, building_key VARCHAR);
INSERT INTO rooms VALUES ('r1', 'b1'), ('r2', 'b1'), ('r3', 'b2');
-- An office's room is a free-text column named like no key.
CREATE TABLE employees (employee_id VARCHAR, office_location VARCHAR, badge INTEGER);
INSERT INTO employees VALUES ('e1', 'r1', 1), ('e2', 'r1', 2), ('e3', 'r3', 3), ('e4', 'r9', 4);
-- Loads repeat a room: its key names no one row.
CREATE TABLE room_loads (room_key VARCHAR, load_date DATE);
INSERT INTO room_loads VALUES ('r1', DATE '2024-01-01'), ('r1', DATE '2024-02-01');
CREATE TABLE visits (visit_id VARCHAR, room_key VARCHAR);
INSERT INTO visits VALUES ('v1', 'r1'), ('v2', 'r2');
"""
EMPLOYEES_BY_BUILDING = """
SELECT b.building_name, COUNT(DISTINCT e.employee_id) FROM employees e
LEFT JOIN rooms r ON r.room_key = e.office_location
LEFT JOIN buildings b ON b.building_key = r.building_key
GROUP BY 1
"""


@pytest.fixture(autouse=True)
def _allow_external_package_paths(monkeypatch):
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _package(root: Path) -> Path:
    pkg = root / "campus"
    (pkg / "data").mkdir(parents=True)
    with duckdb.connect(str(pkg / "data" / "warehouse.duckdb")) as conn:
        conn.execute(SEED)
    _write(
        pkg / "package.yml",
        {
            "schema_version": 1,
            "package": {
                "id": "campus",
                "namespace": "campus",
                "warehouse": "duckdb",
                "default_db": "data/warehouse.duckdb",
                "seed": {"kind": "external"},
            },
        },
    )
    keyed = {
        "building": ("buildings", "building_key", {"building_name": {"kind": "categorical"}}),
        "fac_building": ("fac_buildings", "fac_building_key", {}),
        "room": ("rooms", "room_key", {}),
        "employee": ("employees", "employee_id", {}),
        "room_load": ("room_loads", "room_key", {}),
    }
    _write(
        pkg / "graph.yml",
        {
            "graph": {
                "entities": {
                    name: {"key": [key], "model": relation}
                    for name, (relation, key, _) in keyed.items()
                }
            }
        },
    )
    for name, (relation, _, dimensions) in keyed.items():
        model: dict[str, Any] = {"id": relation, "relation": relation, "entities": {name: {}}}
        if dimensions:
            model["dimensions"] = dimensions
        if name == "employee":
            model["measures"] = {
                "employee_count": {"kind": "entity_count", "entity_key": "employee_id"}
            }
        _write(pkg / "models" / f"{relation}.yml", {"model": model})
    metric = {
        "kind": "aggregate",
        "measure": "measure.campus.employee_count",
        "value_type": "count",
        "description": "Employees",
    }
    _write(pkg / "metrics" / "core.yml", {"metrics": {"employees": metric}})
    return pkg


def _warehouse(tmp_path: Path) -> Path:
    path = tmp_path / "links.duckdb"
    if not path.exists():
        with duckdb.connect(str(path)) as conn:
            conn.execute(SEED)
    return path


def _suggest(tmp_path: Path, relation: str, **options: Any) -> dict[str, Any]:
    with open_duckdb(_warehouse(tmp_path)) as warehouse:
        return suggest_links(warehouse, relation, **options)


def test_a_reporting_table_links_one_to_one_to_its_twin(tmp_path):
    """Every building key is a key of the other building table, and each once: one to one,
    proposed with its evidence in numbers and in words."""
    suggested = _suggest(tmp_path, "buildings")
    assert suggested["threshold"] == {"coverage": 0.95}
    (proposal,) = suggested["proposals"]
    assert (proposal["column"], proposal["target"], proposal["cardinality"]) == (
        "building_key",
        {"relation": "fac_buildings", "key": "fac_building_key"},
        "one_to_one",
    )
    evidence = proposal["evidence"]
    assert (evidence["distinct_found"], evidence["distinct_values"], evidence["coverage"]) == (
        3,
        3,
        1.0,
    )
    assert proposal["reason"] == (
        "3 of 3 distinct building_key values (100.0%) are fac_buildings.fac_building_key keys, "
        "on 3 of 3 rows with a value."
    )


def test_a_partial_link_under_the_threshold_is_listed_with_its_coverage(tmp_path):
    """23 of 25 subjects are in the catalogue: under the stated 95% the link is listed, not
    proposed, with the values not found and the row without one; at 90% it is proposed."""
    suggested = _suggest(tmp_path, "details")
    assert suggested["proposals"] == []
    (listed,) = suggested["below_threshold"]
    evidence = listed["evidence"]
    assert (evidence["coverage"], evidence["distinct_found"], evidence["distinct_values"]) == (
        0.92,
        23,
        25,
    )
    assert (evidence["rows"], evidence["rows_with_value"], evidence["rows_found"]) == (27, 26, 24)
    assert evidence["missing_samples"] == [24, 25]
    assert listed["cardinality"] == "many_to_one"
    assert "1 rows have none; not found: 24, 25." in listed["reason"]
    lowered = _suggest(tmp_path, "details", min_coverage=0.9)
    assert [row["column"] for row in lowered["proposals"]] == ["subject_id"]
    assert lowered["threshold"] == {"coverage": 0.9}


def test_named_columns_are_tried_against_every_key_whatever_their_names(tmp_path):
    """An office's room is in a column named like no key: named, it is tried by its values."""
    assert _suggest(tmp_path, "employees")["proposals"] == []
    suggested = _suggest(tmp_path, "employees", columns=["office_location"])
    (proposal,) = suggested["proposals"] + suggested["below_threshold"]
    assert proposal["target"]["relation"] == "rooms"
    assert (proposal["evidence"]["distinct_found"], proposal["evidence"]["distinct_values"]) == (
        2,
        3,
    )
    with pytest.raises(SemanticLayerError) as refused:
        _suggest(tmp_path, "employees", columns=["no_such_column"])
    assert refused.value.code == "OBJECT_NOT_FOUND"


def _session(server: Any, calls: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    async def run() -> list[dict[str, Any]]:
        async with create_connected_server_and_client_session(server) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            annotations = tools["suggest_links"].annotations
            assert annotations is not None
            assert annotations.readOnlyHint is True and annotations.destructiveHint is False
            return [
                dict((await session.call_tool(name, arguments)).structuredContent or {})
                for name, arguments in calls
            ]

    return asyncio.run(run())


def _files(pkg: Path) -> dict[str, bytes]:
    return {str(path): path.read_bytes() for path in sorted(pkg.rglob("*.yml"))}


def test_a_proposed_link_applies_and_answers_as_the_reference_sql_reads(tmp_path):
    """In a package the targets are its entities. The employee's office links to its room, and
    the room to its building; applied (a preview first, then with the revision), employees by
    building equal the reference SQL, the employee whose office isn't a room in the NULL group.
    Once linked, the pair isn't proposed again."""
    pkg = _package(tmp_path)
    server = create_architect_mcp_server(workspace_root=tmp_path)
    office, room = _session(
        server,
        [
            (
                "suggest_links",
                {"project_path": str(pkg), "relation": "employees", "columns": ["office_location"]},
            ),
            ("suggest_links", {"project_path": str(pkg), "relation": "rooms"}),
        ],
    )
    (to_room,) = office["below_threshold"]  # r9 isn't a room: 2 of 3
    assert to_room["upsert_relationship"] == {
        "from_entity": "employee",
        "to_entity": "room",
        "columns": ["office_location"],
        "cardinality": "many_to_one",
    }
    # A room's building key is a key of both building tables: each is proposed.
    targets = {row["target"]["entity"]: row for row in room["proposals"]}
    assert set(targets) == {"building", "fac_building"}
    to_building = targets["building"]
    assert to_building["upsert_relationship"]["to_entity"] == "building"
    # The loads' key repeats, so nothing can look a load up.
    assert [row["target"]["entity"] for row in room["not_proposed"]] == ["room_load"]

    project = ArchitectProject(pkg, workspace_root=tmp_path)
    before, revision = _files(pkg), project.revision()
    draft = to_room["upsert_relationship"]
    assert project.upsert_relationship(**draft, dry_run=True).report["ok"] is True
    assert _files(pkg) == before
    for index, arguments in enumerate([draft, to_building["upsert_relationship"]]):
        report = project.upsert_relationship(
            **arguments, expected_revision=revision, idempotency_key=f"a1-{index}"
        ).report
        assert report["ok"] is True, report
        revision = project.revision()
    runtime = Runtime.from_path(str(pkg))
    try:
        config = load_package_config(str(pkg))
        name = next(row.id for row in config.dimensions if row.column == "building_name")
        out = runtime.query(
            {
                "select": [{"expression": "measure.campus.employee_count", "as": "v"}],
                "group_by": [name],
            }
        )
        with duckdb.connect(str(pkg / "data" / "warehouse.duckdb"), read_only=True) as conn:
            gold = conn.execute(EMPLOYEES_BY_BUILDING).fetchall()
        answer = [(row[name], row["v"]) for row in out["rows"]]
        assert sorted(answer, key=repr) == sorted(map(tuple, gold), key=repr)
    finally:
        runtime.close()
    (again,) = _session(
        server,
        [
            (
                "suggest_links",
                {"project_path": str(pkg), "relation": "employees", "columns": ["office_location"]},
            )
        ],
    )
    assert again["proposals"] == again["below_threshold"] == []


def test_a_column_is_compared_only_with_keys_of_its_type(tmp_path):
    """An integer badge number is compared with integer keys (the subjects', whose numbers it
    happens to share), never with text room or building keys."""
    suggested = _suggest(tmp_path, "employees", columns=["badge"])
    compared = suggested["proposals"] + suggested["below_threshold"] + suggested["not_proposed"]
    assert {row["target"]["relation"] for row in compared} == {"subjects"}
