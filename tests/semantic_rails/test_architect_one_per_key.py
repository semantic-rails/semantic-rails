"""Architect proposes reading a keyless table one row per a key it repeats, from its data.

A table whose key repeats (a room once per load, a list once per flag) has no key of its own, so
nothing can look anything up in it. ``suggest_one_per_key`` profiles it and, per candidate key,
states the evidence (rows, rows with a NULL key, distinct keys, keys with several rows, and each
other column fixed or varying within a key); it proposes a ``one_per_key`` model only when some
column is fixed per key, with its coverage in words and draft ``upsert_model`` arguments. It
writes nothing: the author applies the drafts with ``dry_run``, then ``expected_revision``, and
nothing checks the data at query time.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

from semantic_rails import architect_introspection as introspection
from semantic_rails.architect_introspection import open_duckdb, suggest_one_per_key
from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_service import ArchitectProject
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime

SEED = """
CREATE TABLE buildings (building_id VARCHAR, building_name VARCHAR);
INSERT INTO buildings VALUES ('b1', 'North Hall'), ('b2', 'South Hall');
-- A room once per load: r1 twice alike, r2 twice with another area, one row with no room.
CREATE TABLE room_loads (
  room_id VARCHAR, building_id VARCHAR, room_use VARCHAR, area DOUBLE, load_date DATE
);
INSERT INTO room_loads VALUES
  ('r1', 'b1', 'Lab', 10.0, DATE '2024-01-01'), ('r1', 'b1', 'Lab', 10.0, DATE '2024-02-01'),
  ('r2', 'b1', 'Office', 12.0, DATE '2024-01-01'), ('r2', 'b1', 'Office', 14.0, DATE '2024-02-01'),
  ('r3', 'b2', 'Lab', 20.0, DATE '2024-01-01'), (NULL, 'b2', 'Storage', 5.0, DATE '2024-01-01');
CREATE TABLE employees (employee_id VARCHAR, office_room_id VARCHAR);
INSERT INTO employees VALUES ('e1', 'r1'), ('e2', 'r1'), ('e3', 'r2'), ('e4', 'r3'), ('e5', 'r9');
-- A list's flags change: every column varies within a list, and two rows are exact copies.
CREATE TABLE list_flags (list_id VARCHAR, flag VARCHAR);
INSERT INTO list_flags VALUES
  ('l1', 'public'), ('l1', 'public'), ('l1', 'hidden'), ('l2', 'public'), ('l2', 'hidden');
-- Unique among the rows with a key, which one row lacks.
CREATE TABLE desks (desk_id VARCHAR, floor_id VARCHAR);
INSERT INTO desks VALUES ('d1', 'f1'), ('d2', 'f1'), (NULL, 'f2');
-- A key of its own.
CREATE TABLE badges (badge_id VARCHAR, tier VARCHAR);
INSERT INTO badges VALUES ('g1', 'gold'), ('g2', 'gold'), ('g3', 'silver');
-- Unique by (list_id, flag_code), and a list's name is fixed per list.
CREATE TABLE lists (list_id VARCHAR, flag_code VARCHAR, list_name VARCHAR);
INSERT INTO lists VALUES
  ('l1', 'public', 'Alpha'), ('l1', 'hidden', 'Alpha'), ('l2', 'public', 'Beta');
"""
EMPLOYEES_BY_BUILDING = """
SELECT b.building_name, COUNT(DISTINCT e.employee_id) FROM employees e
LEFT JOIN (SELECT DISTINCT room_id, building_id FROM room_loads WHERE room_id IS NOT NULL) r
  ON r.room_id = e.office_room_id
LEFT JOIN buildings b ON b.building_id = r.building_id
GROUP BY 1
"""
ROOMS_BY_USE = """
SELECT room_use, COUNT(DISTINCT room_id) FROM room_loads WHERE room_id IS NOT NULL GROUP BY 1
"""


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _package(root: Path) -> Path:
    """A campus package: keyed buildings and employees, and room loads as a row-grain model."""
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
    _write(
        pkg / "graph.yml",
        {
            "graph": {
                "entities": {
                    "building": {"key": ["building_id"], "model": "buildings"},
                    "employee": {"key": ["employee_id"], "model": "employees"},
                }
            }
        },
    )
    count = {"kind": "aggregate", "expr": {"kind": "literal", "value": 1}, "default_agg": "sum"}
    models = {
        "buildings": {
            "relation": "buildings",
            "entities": {"building": {}},
            "dimensions": {"building_name": {"kind": "categorical"}},
        },
        "employees": {
            "relation": "employees",
            "entities": {"employee": {}},
            "measures": {"employee_count": {"kind": "entity_count", "entity_key": "employee_id"}},
        },
        "room_loads": {
            "relation": "room_loads",
            "entities": {},
            "measures": {"load_rows": {**count, "value_type": "count"}},
        },
    }
    for model_id, spec in models.items():
        _write(pkg / "models" / f"{model_id}.yml", {"model": {"id": model_id, **spec}})
    metric = {
        "kind": "aggregate",
        "measure": "measure.campus.employee_count",
        "value_type": "count",
        "description": "Employees",
    }
    _write(pkg / "metrics" / "core.yml", {"metrics": {"employees": metric}})
    return pkg


def _session(server: Any, calls: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    async def run() -> list[dict[str, Any]]:
        async with create_connected_server_and_client_session(server) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            annotations = tools["suggest_one_per_key"].annotations
            assert annotations is not None
            assert annotations.readOnlyHint is True and annotations.destructiveHint is False
            return [
                dict((await session.call_tool(name, arguments)).structuredContent or {})
                for name, arguments in calls
            ]

    return asyncio.run(run())


def _files(pkg: Path) -> dict[str, bytes]:
    return {str(path): path.read_bytes() for path in sorted(pkg.rglob("*.yml"))}


def _gold(pkg: Path, sql: str) -> list[tuple]:
    with duckdb.connect(str(pkg / "data" / "warehouse.duckdb"), read_only=True) as conn:
        return sorted((tuple(row) for row in conn.execute(sql).fetchall()), key=repr)


def _rows(out: dict[str, Any], columns: list[str]) -> list[tuple]:
    return sorted((tuple(row[column] for column in columns) for row in out["rows"]), key=repr)


def _dimension(pkg: Path, entity: str, column: str) -> str:
    config = load_package_config(str(pkg))
    return next(
        row.id for row in config.dimensions if row.entity == entity and row.column == column
    )


@pytest.fixture(autouse=True)
def _allow_external_package_paths(monkeypatch):
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")


def test_a_keyless_table_is_proposed_one_per_key_and_the_proposal_applies(tmp_path):
    """Rooms repeat once per load. The proposal reads them one row per room: building and use
    are fixed per room, area and load date vary and stay row grain, and the row with no room is
    counted, not looked up. Applied (a preview first, then with the revision) and linked, an
    employee's office reaches its building through it, as the reference SQL reads it."""
    pkg = _package(tmp_path)
    server = create_architect_mcp_server(workspace_root=tmp_path)
    (suggested,) = _session(
        server, [("suggest_one_per_key", {"project_path": str(pkg), "relation": "room_loads"})]
    )
    assert (suggested["ok"], suggested["status"]) == (True, "proposed")
    proposal = suggested["proposal"]
    evidence = proposal["evidence"]
    assert {key: evidence[key] for key in evidence if key not in {"fixed", "varying"}} == {
        "columns": ["room_id"],
        "rows": 6,
        "null_key_rows": 1,
        "distinct_keys": 3,
        "keys_with_several_rows": 2,
        "most_rows_per_key": 2,
    }
    assert evidence["fixed"] == ["building_id", "room_use"]
    assert evidence["varying"] == [
        {"column": "area", "keys": 1},
        {"column": "load_date", "keys": 2},
    ]
    assert proposal["coverage"][0] == (
        "5 of 6 rows have a key and read as 3 one-per-key rows; 2 keys have several rows (at "
        "most 2)."
    )
    assert proposal["coverage"][1].startswith("1 rows have no key: no lookup reaches them")
    assert "area (1 keys), load_date (2 keys)" in proposal["coverage"][2]
    draft = proposal["upsert_model"]
    assert (draft["one_per_key"], draft["primary_key"], draft["entity_key"]) == (
        True,
        ["room_id"],
        "room",
    )
    # The building is fixed per room and is the package's building: the room's parent.
    assert draft["entities"] == {"building": {}}
    assert set(draft["dimensions"]) == {"room_use"}
    link = proposal["link"]["upsert_relationship"]
    assert link == {"from_entity": "room_loads", "to_entity": "room", "columns": ["room_id"]}

    project = ArchitectProject(pkg, workspace_root=tmp_path)
    before, revision = _files(pkg), project.revision()
    preview = project.upsert_model(**draft, dry_run=True).report
    assert preview["ok"] is True and _files(pkg) == before  # a preview writes nothing
    with pytest.raises(SemanticLayerError):
        project.upsert_model(**draft, expected_revision="stale", idempotency_key="k1-stale")
    assert _files(pkg) == before
    for index, (write, arguments) in enumerate(
        [
            (project.upsert_model, draft),
            (project.upsert_relationship, link),
            (
                project.upsert_relationship,
                {"from_entity": "employee", "to_entity": "room", "columns": ["office_room_id"]},
            ),
        ]
    ):
        report = write(
            **arguments, expected_revision=revision, idempotency_key=f"k1-{index}"
        ).report
        assert report["ok"] is True, report
        revision = project.revision()

    runtime = Runtime.from_path(str(pkg))
    try:
        building = _dimension(pkg, "entity.campus_building", "building_name")
        by_building = runtime.query(
            {
                "select": [{"expression": "measure.campus.employee_count", "as": "v"}],
                "group_by": [building],
            }
        )
        assert _rows(by_building, [building, "v"]) == _gold(pkg, EMPLOYEES_BY_BUILDING)
        use = _dimension(pkg, "entity.campus_room", "room_use")
        by_use = runtime.query(
            {"select": [{"expression": "measure.campus.room_count", "as": "v"}], "group_by": [use]}
        )
        assert _rows(by_use, [use, "v"]) == _gold(pkg, ROOMS_BY_USE)
    finally:
        runtime.close()


def _suggest(tmp_path: Path, relation: str, key: list[str] | None = None) -> dict[str, Any]:
    pkg = tmp_path / "campus"
    if not pkg.exists():
        _package(tmp_path)
    with open_duckdb(pkg / "data" / "warehouse.duckdb") as warehouse:
        return suggest_one_per_key(warehouse, relation, key)


def test_no_proposal_when_every_column_varies_within_every_key(tmp_path):
    """A list's flags change and two rows are exact copies: no column set is a key, and the
    list key fixes nothing, so nothing is proposed; the evidence says why."""
    suggested = _suggest(tmp_path, "list_flags")
    assert (suggested["status"], suggested["proposal"]) == ("none", None)
    assert suggested["reason"] == (
        "every other column holds two or more values within every candidate key"
    )
    (candidate,) = suggested["candidates"]
    assert candidate["columns"] == ["list_id"]
    assert (candidate["fixed"], candidate["varying"]) == ([], [{"column": "flag", "keys": 2}])


def test_a_key_unique_among_keyed_rows_reads_one_per_key_and_states_its_null_rows(tmp_path):
    """No row repeats a desk, but one row has none: no key of its own, so one row per desk,
    and the row without one is stated, not dropped from the table's own count."""
    suggested = _suggest(tmp_path, "desks")
    assert suggested["status"] == "proposed"
    proposal = suggested["proposal"]
    evidence = proposal["evidence"]
    assert (evidence["null_key_rows"], evidence["keys_with_several_rows"]) == (1, 0)
    assert proposal["coverage"][:2] == [
        "2 of 3 rows have a key and read as 2 one-per-key rows; 0 keys have several rows (at "
        "most 1).",
        "1 rows have no key: no lookup reaches them, and the row-grain model over the same "
        "relation still counts them.",
    ]
    assert proposal["upsert_model"]["primary_key"] == ["desk_id"]


def test_a_table_with_a_key_of_its_own_is_modelled_keyed(tmp_path):
    suggested = _suggest(tmp_path, "badges")
    assert suggested["status"] == "keyed"
    assert suggested["primary_key"]["columns"] == ["badge_id"]
    assert "suggest_model" in suggested["reason"]


def test_a_composite_key_does_not_stop_a_lookup_by_one_of_its_columns(tmp_path):
    """Lists are unique by list and flag, which no other table can look a list up by: a list's
    name is fixed per list, so one row per list is proposed, and the composite key is reported."""
    suggested = _suggest(tmp_path, "lists")
    assert suggested["status"] == "proposed"
    assert suggested["primary_key"]["columns"] == ["list_id", "flag_code"]
    proposal = suggested["proposal"]
    assert proposal["key"] == ["list_id"]
    assert proposal["evidence"]["fixed"] == ["list_name"]


def test_a_named_key_is_tried_alone_and_an_unknown_column_is_refused(tmp_path):
    # A building fixes none of its rooms' columns: nothing to read one row per building.
    suggested = _suggest(tmp_path, "room_loads", ["building_id"])
    (candidate,) = suggested["candidates"]
    assert (candidate["columns"], candidate["fixed"]) == (["building_id"], [])
    assert (suggested["status"], suggested["proposal"]) == ("none", None)
    suggested = _suggest(tmp_path, "room_loads", ["room_id"])
    assert [row["columns"] for row in suggested["candidates"]] == [["room_id"]]
    assert suggested["proposal"]["key"] == ["room_id"]
    with pytest.raises(SemanticLayerError) as refused:
        _suggest(tmp_path, "room_loads", ["no_such_column"])
    assert refused.value.code == "OBJECT_NOT_FOUND"


def test_a_sampled_table_confirms_its_proposal_on_every_row(tmp_path, monkeypatch):
    """Above the profile's row cap, candidates are compared on a sample and the proposal's
    evidence is read again from the whole relation."""
    monkeypatch.setattr(introspection, "MAX_PROFILE_ROWS", 4)
    suggested = _suggest(tmp_path, "room_loads")
    assert (suggested["sampled"], suggested["status"]) == (True, "proposed")
    assert suggested["proposal"]["evidence"]["rows"] == 6


def test_no_table_name_is_a_guess(tmp_path):
    """The draft's entity takes the key's name, and a name the package uses gets a suffix."""
    pkg = _package(tmp_path)
    taken = ArchitectProject(pkg, workspace_root=tmp_path).one_per_key_context("room_loads")
    assert taken["link_model"] == "room_loads"
    assert taken["parents"]["building_id"] == ("building", "building_id")
    with open_duckdb(pkg / "data" / "warehouse.duckdb") as warehouse:
        clash = suggest_one_per_key(
            warehouse, "room_loads", taken=frozenset({"room", uuid.uuid4().hex})
        )
    assert clash["proposal"]["upsert_model"]["entity_key"] == "room_one_per_key"
