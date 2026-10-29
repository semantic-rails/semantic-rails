"""Keyless lookups: a one-per-key model, and the link to the rows of its own relation.

The shop fixture's ``room_rows`` has no key: one stored row per room, department and use. The
``rooms`` model reads that relation with ``one_per_key: true`` (one row per room, each column
its one value), and ``room_rows`` stays row grain beside it with a foreign key to the room: the
link. Its answers are frozen against hand SQL in the shop ledger (``shop/link-*`` and
``shop/one-per-key-*``); this file pins what loads, the SQL a link compiles to, what refuses,
and what the other surfaces say.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantic_rails.architect_service import ArchitectProject
from semantic_rails.config import load_package_config, load_package_snapshot
from semantic_rails.config_validation import resolve_package_reference
from semantic_rails.dialects import supported_warehouses
from semantic_rails.errors import SemanticLayerError
from semantic_rails.interop.package_writer import write_package
from semantic_rails.metadata import inspect_payload
from semantic_rails.package_tools import check_warehouse_column_reachability_report
from semantic_rails.relation_pipelines import lower_relation
from semantic_rails.runtime import Runtime
from semantic_rails.sql_preparation import _shortened_identifier
from tests.semantic_rails.dbt_warehouse import build_dbt_warehouse, write_orders_package

SHOP = Path(__file__).resolve().parents[1] / "integration" / "correctness" / "shop"
ROOM = "entity.shop_room"
ROOM_ROWS = "entity.shop_room_rows"
DEPARTMENT = "dimension.shop_room_rows_department"
ROOM_USE = "dimension.shop_room_rows_room_use"
HEADCOUNT = "measure.shop.headcount"
ROOM_COUNT = "measure.shop.room_count"


def _shop(tmp_path: Path, *edits: tuple[str, Any]) -> Path:
    """A copy of the shop fixture, each ``(file, edit)`` applied to that YAML document."""
    package = tmp_path / "shop"
    shutil.copytree(SHOP, package)
    for name, edit in edits:
        path = package / name
        path.parent.mkdir(exist_ok=True)
        document = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        edit(document)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return package


def _rooms(edit: Any) -> tuple[str, Any]:
    return "models/rooms.yml", lambda document: edit(document["model"])


def _runtime(package: Path) -> Runtime:
    config = load_package_config(str(package))
    return Runtime.from_config(config, source_path=str(package), package_id="shop")


@pytest.fixture(scope="module")
def shop(tmp_path_factory: pytest.TempPathFactory) -> Any:
    runtime = _runtime(_shop(tmp_path_factory.mktemp("keyless")))
    runtime.query({"select": [ROOM_COUNT]})  # the fixture opens the warehouse, and closes it
    yield runtime
    runtime.close()


def _refusal(runtime: Runtime, query: dict[str, Any]) -> SemanticLayerError:
    with pytest.raises(SemanticLayerError) as raised:
        runtime.query(query)
    return raised.value


def _sql(runtime: Runtime, query: dict[str, Any]) -> str:
    return " ".join(str(runtime.compile(query)["rendered_sql"]).split())


def test_a_one_per_key_model_reads_its_relation_once_per_key() -> None:
    config = load_package_config(str(SHOP))
    room = next(entity for entity in config.entities if entity.id == ROOM)
    assert (room.one_per_key, room.key_columns, room.is_row_grain) == (True, ["room_id"], False)
    relation = next(row for row in config.relations if row.id == room.relation_id)
    assert room.table == relation.output_name == "sr_one_per_key__shop_room"
    columns = ["building", "floor", "accessible", "area"]
    assert [(step.kind, step.config) for step in relation.steps] == [
        ("source", {"relation": "room_rows", "columns": ["room_id", *columns]}),
        ("where", {"predicates": [{"field": "room_id", "op": "missing", "value": False}]}),
        (
            "group_by",
            {
                "dimensions": {"room_id": "room_id"},
                "aggregates": {
                    column: {
                        "function": "single_value",
                        "expr": column,
                        **({"boolean": True} if column == "accessible" else {}),
                    }
                    for column in columns
                },
            },
        ),
    ]
    rows = next(entity for entity in config.entities if entity.id == ROOM_ROWS)
    assert rows.is_row_grain and not rows.one_per_key and rows.table == "room_rows"


def test_only_the_row_grain_sibling_on_the_same_relation_is_a_link() -> None:
    config = load_package_config(str(SHOP))
    into_rooms = {
        row.source_entity: row.link for row in config.relationships if row.target_entity == ROOM
    }
    assert into_rooms == {
        ROOM_ROWS: True,  # row grain, on room_rows, by the room's key
        "entity.shop_person": False,  # keyed, on its own table
        "entity.shop_room_bookings": False,  # row grain, but on its own table
    }


@pytest.mark.parametrize(
    ("edits", "message"),
    [
        (
            [_rooms(lambda model: model.update(one_per_key="yes"))],
            "model 'rooms' one_per_key must be true or false",
        ),
        (  # no graph entity is homed on it, so it has no key to read one row per
            [
                ("graph.yml", lambda doc: doc["graph"]["entities"].pop("room")),
                ("models/room_rows.yml", lambda doc: doc["model"].update(entities={})),
                ("models/people.yml", lambda doc: doc["model"]["entities"].pop("room")),
                ("models/room_bookings.yml", lambda doc: doc["model"].update(entities={})),
                _rooms(lambda model: model.update(entities={})),
            ],
            "model 'rooms' is one_per_key, which reads its relation as one row per its "
            "entity's key, but no graph entity is homed on it",
        ),
        (  # a stored rollup would answer from rows the one-per-key reading never sees
            [
                _rooms(
                    lambda model: model.update(
                        variants={
                            "monthly": {
                                "relation": "rooms_monthly",
                                "grain": {"time": "month", "entities": ["room"]},
                            }
                        }
                    )
                )
            ],
            "model 'rooms' is one_per_key, so it can't have variants: a stored rollup isn't read "
            "one row per key",
        ),
    ],
)
def test_what_a_one_per_key_model_refuses_to_load(tmp_path, edits, message):
    with pytest.raises(SemanticLayerError) as raised:
        load_package_config(str(_shop(tmp_path, *edits)))
    assert raised.value.code == "INVALID_CONFIG"
    assert message in str(raised.value)


def test_a_row_filter_on_a_one_per_key_dimension_is_refused(tmp_path):
    policy = {"id": "policy.shop.own_building", "kind": "row_filter"}
    policy.update(dimension="dimension.shop_room_building", attribute="building", type="string")
    package = _shop(tmp_path, ("policies.yml", lambda doc: doc.update(semantic_policies=[policy])))
    with pytest.raises(SemanticLayerError) as raised:
        load_package_config(str(package))
    assert raised.value.code == "INVALID_CONFIG"
    assert "must be a plain column of its entity's relation" in str(raised.value)


# The aggregates that read a true/false group's lowest and highest value, where MIN and MAX
# aren't documented to take a boolean. Postgres takes no MIN or MAX of a boolean, and a column
# only a measure reads has no declared type, so it reads every column's least and greatest value
# by sorting (ARRAY_AGG ... ORDER BY).
BOOLEAN_BOUNDS = {
    "athena": ("BOOL_AND", "BOOL_OR"),
    "bigquery": ("LOGICAL_AND", "LOGICAL_OR"),
    "snowflake": ("BOOLAND_AGG", "BOOLOR_AGG"),
}


def _on(warehouse: str, *edits: tuple[str, Any], tmp_path: Path) -> Any:
    config = load_package_config(str(_shop(tmp_path, *edits)))
    return replace(config, package=replace(config.package, warehouse=warehouse))


def _single_value_sql(config: Any, query: dict[str, Any]) -> str:
    """The rendered statement, from its start through the one-per-key CTE."""
    runtime = Runtime.from_config(config, source_path=str(SHOP), package_id="shop")
    try:
        sql = " ".join(str(runtime.compile(query)["rendered_sql"]).split())
    finally:
        runtime.close()
    return sql[: sql.index("GROUP BY src.room_id")]


@pytest.mark.parametrize("warehouse", sorted(set(supported_warehouses()) - {"postgres"}))
def test_a_boolean_column_reads_its_one_value_on_every_warehouse(tmp_path, warehouse):
    config = _on(warehouse, tmp_path=tmp_path)
    room = next(entity for entity in config.entities if entity.id == ROOM)
    relation = next(row for row in config.relations if row.id == room.relation_id)
    ctes, _ = lower_relation(relation, warehouse=warehouse)
    fields = {field.alias: field.expression for field in ctes[-1].query.select}
    least, most = BOOLEAN_BOUNDS.get(warehouse, ("MIN", "MAX"))
    for column, (low, high) in {"accessible": (least, most), "area": ("MIN", "MAX")}.items():
        [when] = fields[column].whens
        assert (when.condition.left.name, when.condition.right.name) == (low, high), column
        assert when.result.name == high
    sql = _single_value_sql(
        config, {"select": [ROOM_COUNT], "group_by": ["dimension.shop_room_accessible"]}
    )
    assert f"{least}(" in sql.upper() and f"{most}(" in sql.upper()


@pytest.mark.parametrize(
    "edits",
    [
        [],  # the boolean is a dimension
        [  # only a measure reads it, so no dimension declares its type
            _rooms(lambda model: model["dimensions"].pop("accessible")),
            _rooms(
                lambda model: model["measures"].update(
                    accessible_rooms={
                        "kind": "aggregate",
                        "expr": {
                            "kind": "case",
                            "whens": [
                                {
                                    "when": {
                                        "kind": "comparison",
                                        "op": "=",
                                        "left": {"kind": "column", "column": "accessible"},
                                        "right": {"kind": "literal", "value": True},
                                    },
                                    "then": {"kind": "literal", "value": 1},
                                }
                            ],
                            "else": {"kind": "literal", "value": 0},
                        },
                    }
                )
            ),
        ],
    ],
)
def test_postgres_reads_every_one_per_key_column_without_min_or_max(tmp_path, edits):
    """Any type, booleans included, and only its distinct values held: never a sorted array of
    every row."""
    config = _on("postgres", *edits, tmp_path=tmp_path)
    sql = _single_value_sql(config, {"select": [ROOM_COUNT]})
    assert "MIN(" not in sql.upper() and "MAX(" not in sql.upper() and "ORDER BY" not in sql
    for column in ("building", "floor", "accessible", "area"):
        assert (
            f"CASE WHEN COUNT(DISTINCT src.{column}) = 1 THEN (ARRAY_AGG(DISTINCT src.{column}) "
            f"FILTER (WHERE src.{column} IS NOT NULL))[1] END AS {column}"
        ) in sql, column


def test_a_parent_count_across_a_link_keeps_the_link_rules(tmp_path):
    """A distinct count of the room key, with the link declared safe to
    count in reverse, still overlaps across departments: its share refuses, and its rows count
    each room once in every department."""
    distinct = {"kind": "entity_count", "entity_key": "room_id", "accumulation": {"kind": "flow"}}
    link = {
        "entities": ["room_rows", "room"],
        "cardinality": "many_to_one",
        "rollup_safe": {"reverse": ["count_distinct"]},
    }
    package = _shop(
        tmp_path,
        _rooms(lambda model: model["measures"].update(distinct_rooms=distinct)),
        ("graph.yml", lambda doc: doc["graph"]["relationships"].update(room_link=link)),
    )
    runtime = _runtime(package)
    try:
        share = {"kind": "share", "input": "measure.shop.distinct_rooms", "over": [DEPARTMENT]}
        error = _refusal(
            runtime, {"select": [{"as": "share", "expression": share}], "group_by": [DEPARTMENT]}
        )
        result = runtime.query(
            {"select": ["measure.shop.distinct_rooms"], "group_by": [DEPARTMENT]}
        )
    finally:
        runtime.close()
    assert error.code == "MIXED_GRAIN_INVALID"
    assert "A share across a link is not supported" in error.details["why_invalid"]
    assert {row[DEPARTMENT]: row["distinct_rooms"] for row in result["rows"]} == {
        None: 1,
        "design": 2,
        "finance": 2,
        "legal": 1,
    }
    [step] = [w for w in result["warnings"] if w["code"] == "REWRITE_APPLIED"]
    assert "the groups can overlap and need not add up to the total" in str(step)


GENERATED = "sr_one_per_key__shop_room"  # the name rooms' one-per-key relation is read under


@pytest.mark.parametrize(
    "table",
    [
        GENERATED,
        f"  {GENERATED.upper()}  ",  # padded, in another case
        f"main.{GENERATED}",  # in a schema
        GENERATED.replace("_", "-", 3),  # a name a warehouse would make legal by underscores
        f"{GENERATED}__2_where",  # one of its steps
        _shortened_identifier("sr_one_per_key__" + "r" * 64),  # as Postgres shortens one
        "sr_one_per_key",  # whose steps would be read under sr_one_per_key__2_where
        "Sr-One-Per-Key-Rows",
    ],
)
def test_a_name_one_per_key_relations_are_read_under_can_t_be_authored(tmp_path, table):
    """An authored table or relation read under a generated name would read the one-per-key rows
    instead. Every generated name starts with the
    reserved prefix, shortened or not, and every authored name holding it is refused."""
    renamed = ("models/people.yml", lambda doc: doc["model"].update(relation=table))
    with pytest.raises(SemanticLayerError) as raised:
        load_package_config(str(_shop(tmp_path, renamed)))
    assert raised.value.code == "INVALID_CONFIG"
    assert "sr_one_per_key is reserved" in str(raised.value)
    assert raised.value.details["name"] == table


def test_a_pipeline_source_padded_to_a_generated_name_is_refused(tmp_path):
    """A pipeline's source is stripped where it is read, so a padded one counts as unpadded."""
    staff = {
        "columns": ["person_id", "room_id", "title", "salary", "started_on"],
        "steps": [{"source": f" {GENERATED} "}],
    }
    package = _shop(
        tmp_path,
        ("relations.yml", lambda doc: doc.update(relations={"staff": staff})),
        ("models/people.yml", lambda doc: doc["model"].update(relation="staff")),
    )
    with pytest.raises(SemanticLayerError) as raised:
        load_package_config(str(package))
    assert raised.value.code == "INVALID_CONFIG"
    assert "sr_one_per_key is reserved" in str(raised.value)


def test_a_pipeline_whose_steps_are_read_under_a_generated_name_is_refused(tmp_path):
    """A pipeline output named sr_one_per_key has a step read under
    sr_one_per_key__2_where, the name of a room relation for an entity id ending 2_where."""
    staff = {
        "output_name": "sr_one_per_key",
        "columns": ["person_id", "room_id", "title", "salary", "started_on"],
        "steps": [{"source": "people"}, {"where": []}, {"select": {"room_id": "room_id"}}],
    }
    package = _shop(
        tmp_path,
        (
            "graph.yml",
            lambda doc: doc["graph"]["entities"]["room"].update({"as": "entity.2_where"}),
        ),
        ("relations.yml", lambda doc: doc.update(relations={"staff": staff})),
        ("models/people.yml", lambda doc: doc["model"].update(relation="staff")),
    )
    with pytest.raises(SemanticLayerError) as raised:
        load_package_config(str(package))
    assert raised.value.code == "INVALID_CONFIG"
    assert raised.value.details["name"] == "sr_one_per_key"
    assert "sr_one_per_key is reserved" in str(raised.value)


def test_every_generated_name_keeps_the_reserved_prefix_once_shortened(tmp_path):
    """A 64-character entity id's generated names, shortened as Postgres
    shortens an identifier, still start with the reserved prefix; the old shortened name is an
    ordinary table and reads as one."""
    long_id = "entity." + "r" * 64
    package = _shop(
        tmp_path,
        ("graph.yml", lambda doc: doc["graph"]["entities"]["room"].update({"as": long_id})),
    )
    seed = package / "data" / "seed.sql"
    old = _shortened_identifier("r" * 64 + "__one_per_key")
    seed.write_text(
        seed.read_text(encoding="utf-8") + f"\nALTER TABLE people RENAME TO {old};\n",
        encoding="utf-8",
    )
    people = package / "models" / "people.yml"
    people.write_text(
        people.read_text(encoding="utf-8").replace("relation: people", f"relation: {old}"),
        encoding="utf-8",
    )
    config = load_package_config(str(package))
    room = next(entity for entity in config.entities if entity.id == long_id)
    relation = next(row for row in config.relations if row.id == room.relation_id)
    ctes, _ = lower_relation(relation, warehouse="postgres")
    for name in [relation.output_name, *(cte.name for cte in ctes)]:
        shortened = _shortened_identifier(name) if len(name.encode()) > 63 else name
        assert shortened.startswith("sr_one_per_key__"), name
    runtime = Runtime.from_config(config, source_path=str(package), package_id="shop")
    try:
        rows = runtime.query({"select": [HEADCOUNT, ROOM_COUNT]})["rows"]
    finally:
        runtime.close()
    assert rows == [{"headcount": 7, "room_count": 5}]


def _aggregate_if(value: str) -> dict[str, Any]:
    accessible = {"kind": "column", "column": "accessible", "entity": ROOM}
    return {
        "as": "kept",
        "expression": {
            "kind": "aggregate_if",
            "aggregation": "sum",
            "condition": {
                "kind": "comparison",
                "op": "=",
                "left": accessible,
                "right": {"kind": "literal", "value": True},
            },
            "value": {"kind": "column", "column": value, "entity": ROOM},
        },
    }


def test_an_aggregate_if_reads_only_the_columns_a_one_per_key_model_declares(tmp_path):
    """A column the model declares nothing on isn't in its rows, so the
    call is refused while binding, naming the declaration that adds it."""
    package = _shop(tmp_path)
    seed = package / "data" / "seed.sql"
    seed.write_text(
        seed.read_text(encoding="utf-8")
        + "\nALTER TABLE room_rows ADD COLUMN bonus INTEGER;\nUPDATE room_rows SET bonus = 42;\n",
        encoding="utf-8",
    )
    runtime = _runtime(package)
    try:
        report = runtime.validate({"select": [_aggregate_if("bonus")]})
        error = _refusal(runtime, {"select": [_aggregate_if("bonus")]})
        declared = runtime.query({"select": [_aggregate_if("area")]})["rows"]
    finally:
        runtime.close()
    assert not report["ok"]
    assert report["errors"][0]["code"] == error.code == "UNSUPPORTED_CONDITIONAL_AGGREGATE"
    assert "'bonus'" in str(error)
    [hint] = error.details["recovery_hints"]
    assert hint["kind"] == "declare_column"
    assert "declare a dimension or measure on 'bonus'" in hint["message"]
    assert declared == [{"kept": 200}]  # r1 and r3, 100 each


def test_a_measure_on_another_model_reads_only_declared_one_per_key_columns(tmp_path):
    """Any measure, on any model, reading a column a one-per-key model
    doesn't project is refused while planning, with the same hint; a declared one reads."""
    people_measures = {
        "room_bonus": {
            "kind": "aggregate",
            "expr": {"kind": "column", "entity": ROOM, "column": "bonus"},
        },
        "room_area_seen": {
            "kind": "aggregate",
            "expr": {"kind": "column", "entity": ROOM, "column": "area"},
        },
    }
    package = _shop(
        tmp_path,
        ("models/people.yml", lambda doc: doc["model"]["measures"].update(people_measures)),
    )
    seed = package / "data" / "seed.sql"
    seed.write_text(
        seed.read_text(encoding="utf-8")
        + "\nALTER TABLE room_rows ADD COLUMN bonus INTEGER;\nUPDATE room_rows SET bonus = 42;\n",
        encoding="utf-8",
    )
    runtime = _runtime(package)
    try:
        report = runtime.validate({"select": ["measure.shop.room_bonus"]})
        error = _refusal(runtime, {"select": ["measure.shop.room_bonus"]})
        declared = runtime.query({"select": ["measure.shop.room_area_seen"]})["rows"]
    finally:
        runtime.close()
    assert not report["ok"]
    assert report["errors"][0]["code"] == error.code == "INVALID_CONFIG"
    assert "Measure 'measure.shop.room_bonus' reads column 'bonus' of 'entity.shop_room'" in str(
        error
    )
    [hint] = error.details["recovery_hints"]
    assert hint["kind"] == "declare_column"
    assert "declare a dimension or measure on 'bonus'" in hint["message"]
    assert declared == [{"room_area_seen": 650}]  # each person's room once: 100+100+300+100+50


def test_a_lookup_into_a_one_per_key_model_on_another_column_reads_it(tmp_path):
    """A lookup into a one-per-key model on a column it declares no dimension for."""
    package = _shop(
        tmp_path,
        (
            "graph.yml",
            lambda doc: doc["graph"]["relationships"].update(
                people_room={
                    "entities": ["person", "room"],
                    "via": ["room_id"],
                    "target": ["external_id"],
                    "cardinality": "many_to_one",
                }
            ),
        ),
    )
    seed = package / "data" / "seed.sql"
    seed.write_text(
        seed.read_text(encoding="utf-8")
        + "\nALTER TABLE room_rows ADD COLUMN external_id VARCHAR(8);\n"
        + "UPDATE room_rows SET external_id = room_id;\n",
        encoding="utf-8",
    )
    runtime = _runtime(package)
    try:
        rows = runtime.query({"select": [HEADCOUNT], "group_by": ["dimension.shop_room_building"]})
    finally:
        runtime.close()
    assert {row["dimension.shop_room_building"]: row["headcount"] for row in rows["rows"]} == {
        None: 2,
        "north": 3,
        "south": 2,
    }


def test_a_link_that_forbids_the_reverse_walk_names_the_setting(tmp_path):
    forward_only = (
        "package.yml",
        lambda doc: doc.setdefault("defaults", {}).update(relationship={"traversal": ["forward"]}),
    )
    runtime = _runtime(_shop(tmp_path / "forward", forward_only))
    try:
        error = _refusal(runtime, {"select": [ROOM_COUNT], "group_by": [DEPARTMENT]})
    finally:
        runtime.close()
    assert error.code == "PATH_NOT_FOUND"
    assert error.details["relationship"] == "relationship.room_rows_room"
    assert str(error) == (
        "No path from 'entity.shop_room' to 'entity.shop_room_rows': link relationship "
        "'relationship.room_rows_room' (Room rows to Room) allows only forward, and its rows "
        "are reached by walking it in reverse. Set allowed_directions: [forward, reverse] on a "
        "graph.relationships row for it."
    )
    reverse = {"entities": ["room_rows", "room"], "allowed_directions": ["forward", "reverse"]}
    allowed = ("graph.yml", lambda doc: doc["graph"]["relationships"].update(room_link=reverse))
    runtime = _runtime(_shop(tmp_path / "allowed", forward_only, allowed))
    try:
        rows = runtime.query({"select": [ROOM_COUNT], "group_by": [DEPARTMENT]})["rows"]
    finally:
        runtime.close()
    assert {row[DEPARTMENT]: row["room_count"] for row in rows} == {
        None: 1,
        "design": 2,
        "finance": 2,
        "legal": 1,
    }


def test_a_grouping_across_a_link_keeps_one_row_per_entity_and_joins_left(shop):
    sql = _sql(shop, {"select": [HEADCOUNT], "group_by": [DEPARTMENT]})
    assert "SELECT DISTINCT people.person_id AS __entity_key_1, room_rows.department AS g1" in sql
    assert (
        "FROM people LEFT JOIN sr_one_per_key__shop_room ON people.room_id = "
        "sr_one_per_key__shop_room.room_id LEFT JOIN room_rows ON "
        "sr_one_per_key__shop_room.room_id = room_rows.room_id"
    ) in sql
    result = shop.query({"select": [HEADCOUNT], "group_by": [DEPARTMENT]})
    [step] = [w for w in result["warnings"] if w["code"] == "REWRITE_APPLIED"]
    assert "the groups can overlap and need not add up to the total" in str(step)


def test_a_filter_across_a_link_stays_a_semi_join(shop):
    query = {"select": [HEADCOUNT], "where": [{"field": DEPARTMENT, "op": "is", "value": "design"}]}
    sql = _sql(shop, query)
    assert "EXISTS" in sql and "JOIN room_rows" not in sql
    assert shop.query(query)["rows"] == [{"headcount": 3}]


def test_two_groupings_across_one_link_read_one_stored_row(shop):
    """r1 is a design lab and a finance office: its two people are never in a design office."""
    rows = shop.query({"select": [HEADCOUNT], "group_by": [DEPARTMENT, ROOM_USE]})["rows"]
    assert sorted(rows, key=str) == sorted(
        [
            {DEPARTMENT: "design", ROOM_USE: "lab", "headcount": 2},
            {DEPARTMENT: "design", ROOM_USE: "office", "headcount": 1},
            {DEPARTMENT: "finance", ROOM_USE: "office", "headcount": 3},
            {DEPARTMENT: None, ROOM_USE: "storage", "headcount": 1},
            {DEPARTMENT: None, ROOM_USE: None, "headcount": 2},
        ],
        key=str,
    )


@pytest.mark.parametrize(
    ("query", "why"),
    [
        (  # the groups overlap, so a share of their sum is no share of the people
            {
                "select": [
                    {
                        "as": "share",
                        "expression": {"kind": "share", "input": HEADCOUNT, "over": [DEPARTMENT]},
                    }
                ],
                "group_by": [DEPARTMENT],
            },
            "A share across a link is not supported",
        ),
        (
            {
                "select": [
                    {"as": "running", "expression": {"kind": "cumulative", "input": HEADCOUNT}}
                ],
                "group_by": [DEPARTMENT],
                "time": {"temporal_role": "temporal_role.shop_person_started_on", "grain": "month"},
            },
            "A window, per-entity rollup, distribution or metric predicate",
        ),
        (  # bookings have no key: nothing counts each stored row once per department
            {"select": ["measure.shop.bookings"], "group_by": [DEPARTMENT]},
            "is not defined over one row per 'entity.shop_room_bookings' key",
        ),
        (  # not a link: bookings are not on the rooms' relation
            {"select": [HEADCOUNT], "group_by": ["dimension.shop_room_bookings_booker"]},
            "is many-to-many (a lookup before the one-to-many hop",
        ),
        (  # a room counts whole under each of its bookers (the ledger's
            # shop/one-per-key-rooms-by-booker); its largest area has no such reading
            {
                "select": [
                    {
                        "as": "largest",
                        "expression": {
                            "kind": "measure",
                            "measure": "measure.shop.room_area",
                            "aggregation": "max",
                        },
                    }
                ],
                "group_by": ["dimension.shop_room_bookings_booker"],
            },
            "is ambiguous across a one-to-many hop",
        ),
        (
            {
                "select": [ROOM_COUNT],
                "group_by": [DEPARTMENT],
                "where": [{"field": ROOM_USE, "op": "not", "value": "office"}],
            },
            "A negated or null test on 'dimension.shop_room_rows_room_use'",
        ),
    ],
)
def test_what_still_refuses_across_a_link(shop, query, why):
    error = _refusal(shop, query)
    assert error.code == "MIXED_GRAIN_INVALID"
    assert why in error.details["why_invalid"]


def test_two_filters_across_a_link_still_ask_which_rows(shop):
    error = _refusal(
        shop,
        {
            "select": [HEADCOUNT],
            "where": [
                {"field": DEPARTMENT, "op": "is", "value": "design"},
                {"field": ROOM_USE, "op": "is", "value": "office"},
            ],
        },
    )
    assert error.code == "AMBIGUOUS_CHILD_SCOPE"
    options = error.details["clarification"]["options"]
    assert [option["id"] for option in options] == ["same_row", "separate_rows"]


def test_the_cards_say_one_row_per_key_and_name_the_link(shop):
    room = inspect_payload(shop, object_id=ROOM)["card"]
    assert room["key"] == ["room_id"]
    assert room["key_note"] == (
        "One row per room_id, read from room_rows. A column with several values within a key "
        "reads NULL."
    )
    rows = inspect_payload(shop, object_id=ROOM_ROWS)["card"]
    assert rows["key_note"] == (
        "No key: every stored row counts. It looks up Room. No model can join to one of its "
        "rows by key, but its measures still group and filter by its own columns and through "
        "Room, beside other models' measures. Its columns are reached from Room one to many: "
        "each counts once in every group it has a row in."
    )


def test_validate_reads_a_one_per_key_model_s_columns_from_its_relation(tmp_path):
    misspelled = _rooms(lambda model: model["dimensions"].update(wing={"kind": "categorical"}))
    runtime = _runtime(_shop(tmp_path, misspelled))
    try:
        ref = resolve_package_reference(path=runtime.snapshot.source_path)
        report = check_warehouse_column_reachability_report(ref, runtime=runtime)
    finally:
        runtime.close()
    [missing] = [error for error in report["errors"] if error["details"].get("column") == "wing"]
    assert missing["code"] == "WAREHOUSE_COLUMN_NOT_FOUND"
    assert (missing["details"]["dimension_id"], missing["details"]["table"]) == (
        "dimension.shop_room_wing",
        "room_rows",
    )


def test_the_writer_keeps_a_one_per_key_model(tmp_path):
    source = tmp_path / "source" / "rooms"
    documents = {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "rooms",
                "namespace": "rooms",
                "default_db": "data/rooms.duckdb",
                "seed": {"kind": "external"},
            },
        },
        "graph.yml": {"graph": {"entities": {"room": {"key": ["room_id"], "model": "rooms"}}}},
        **{
            f"models/{name}.yml": yaml.safe_load((SHOP / f"models/{name}.yml").read_text())
            for name in ("rooms", "room_rows")
        },
    }
    for name, document in documents.items():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    snapshot = load_package_snapshot(source)
    assert [row.link for row in snapshot.config.relationships] == [True]
    written = write_package(snapshot.config, tmp_path / "written" / "rooms", namespace="rooms")
    assert load_package_snapshot(written).semantic == snapshot.semantic
    model = yaml.safe_load((written / "models" / "room.yml").read_text(encoding="utf-8"))
    assert (model["model"]["relation"], model["model"]["one_per_key"]) == ("room_rows", True)
    assert "link" not in (written / "graph.yml").read_text(encoding="utf-8")


def test_the_architect_declares_a_one_per_key_model(tmp_path):
    project = write_orders_package(tmp_path, seed={"kind": "external"}, with_customers=False)
    build_dbt_warehouse(project / "data" / "warehouse.duckdb")
    architect = ArchitectProject(project, workspace_root=tmp_path)
    result = architect.upsert_model(
        model_id="order_customers",
        entity_key="order_customer",
        relation="main_marts.fct_orders",
        primary_key=["customer_id"],
        one_per_key=True,
        dimensions={"status": {"kind": "categorical"}},
    )
    assert result.report["ok"] is True, result.report
    written = project / result.report["target_file"]
    assert yaml.safe_load(written.read_text(encoding="utf-8"))["model"]["one_per_key"] is True
    config = load_package_config(str(project))
    customers = next(row for row in config.entities if row.id == "entity.shop_order_customer")
    assert customers.one_per_key and customers.table == "sr_one_per_key__shop_order_customer"
    with pytest.raises(SemanticLayerError) as raised:
        architect.upsert_model(
            model_id="order_rows",
            entity_key="order_row",
            relation="main_marts.fct_orders",
            primary_key=[],
            one_per_key=True,
        )
    assert raised.value.code == "INVALID_CONFIG"
    assert "needs a primary_key" in str(raised.value)
