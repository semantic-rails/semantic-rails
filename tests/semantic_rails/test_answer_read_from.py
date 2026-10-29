"""An answer says which model each of its values comes from (``read_from``), before its rows.

A call reads measures, metrics and dimensions that may sit on different models, joined through
relationships the call never names. Two models can hold the same kind of value (a room number on
a rooms table and on a space-detail table), so an answer read from the other one looks right.
``execute`` answers say, by id, which model each selected value and each group_by or where
dimension was read from, and the relationships the SQL took to reach a model when nothing else
in the response states that route. A model that ``routes`` already names is left out. The
answer itself never changes, and a caller who can't see a model is told exactly what a package
without it says.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace as Row
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.answer import _stated_routes, read_from
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime
from tests.semantic_rails import test_listings_one_to_many as listings
from tests.semantic_rails.hidden_absent import (
    CALLER,
    absent,
    envelope,
    mcp_call,
    visibility_policy,
    with_policies,
)
from tests.semantic_rails.test_runtime_and_metadata import jaffle_snapshot as jaffle_snapshot

ROOM, BUILDING, BOOKING, WING = (
    f"entity.campus_{name}" for name in ("room", "building", "booking", "wing")
)
ROOMS_AREA = "measure.campus.rooms_area"
ROOM_COUNT = "measure.campus.rooms_count"
BOOKED_HOURS = "measure.campus.bookings_hours"
AREA_PER_BOOKING = "metric.campus.area_per_booking"
BUILDING_NAME = "dimension.campus_building_name"
ROOM_KIND = "dimension.campus_room_kind"
PURPOSE = "dimension.campus_booking_purpose"
ROOM_BUILDING = "relationship.room_building"
BOOKING_ROOM = "relationship.booking_room"
SEED = """
CREATE TABLE buildings (building_id INTEGER, name VARCHAR);
INSERT INTO buildings VALUES (1, 'Hall'), (2, 'Annex');
CREATE TABLE wings (wing_id INTEGER, building_id INTEGER, name VARCHAR);
INSERT INTO wings VALUES (1, 1, 'East'), (2, 2, 'West');
CREATE TABLE rooms (room_id INTEGER, building_id INTEGER, wing_id INTEGER, kind VARCHAR,
  area DOUBLE);
INSERT INTO rooms VALUES (1, 1, 1, 'office', 30), (2, 1, 1, 'lab', 20), (3, 2, 2, 'office', 50);
CREATE TABLE bookings (booking_id INTEGER, room_id INTEGER, purpose VARCHAR, hours DOUBLE);
INSERT INTO bookings VALUES (1, 1, 'exam', 2), (2, 1, 'class', 3), (3, 3, 'exam', 1);
"""


def _model(model_id: str, entities: list[str], dimensions: list[str], measures: dict[str, Any]):
    return {
        "model": {
            "id": model_id,
            "relation": model_id,
            "entities": {entity: {} for entity in entities},
            "dimensions": {name: {"kind": "categorical"} for name in dimensions},
            "measures": measures,
        }
    }


def _sum(label: str, column: str) -> dict[str, Any]:
    return {
        "label": label,
        "kind": "aggregate",
        "expr": column,
        "accumulation": {"kind": "flow"},
        "value_type": "number",
    }


def _count(label: str, key: str) -> dict[str, Any]:
    return {
        "label": label,
        "kind": "entity_count",
        "entity_key": key,
        "accumulation": {"kind": "population"},
        "value_type": "count",
    }


def _files(wings: bool) -> dict[str, Any]:
    """Rooms in buildings, and the bookings of each room; ``wings`` adds a second route from a
    room to its building, through the wing it is in."""
    entities = {
        "building": {"key": ["building_id"], "model": "buildings"},
        "room": {"key": ["room_id"], "model": "rooms"},
        "booking": {"key": ["booking_id"], "model": "bookings"},
    }
    relationships = {
        "room_building": {"entities": ["room", "building"], "cardinality": "many_to_one"},
        "booking_room": {"entities": ["booking", "room"], "cardinality": "many_to_one"},
    }
    room_entities = ["room", "building"]
    files: dict[str, Any] = {
        "package.yml": {
            "schema_version": 1,
            "package": {
                "id": "campus",
                "namespace": "campus",
                "name": "Campus",
                "description": "Rooms and their bookings",
                "warehouse": "duckdb",
                "default_db": "campus.duckdb",
                "seed": {"kind": "external"},
            },
            "defaults": {"time": {"timezone": "UTC"}},
        },
        "models/buildings.yml": _model("buildings", ["building"], ["name"], {}),
        "models/bookings.yml": _model(
            "bookings",
            ["booking", "room"],
            ["purpose"],
            {
                "bookings_hours": _sum("Booked hours", "hours"),
                "bookings_count": _count("Bookings", "booking_id"),
            },
        ),
        "metrics/campus.yml": {
            "metrics": {
                "area_per_booking": {
                    "label": "Area per booking",
                    "kind": "ratio",
                    "numerator": "rooms_area",
                    "denominator": "bookings_count",
                    "value_type": "number",
                }
            }
        },
    }
    if wings:
        entities["wing"] = {"key": ["wing_id"], "model": "wings"}
        relationships["room_wing"] = {"entities": ["room", "wing"], "cardinality": "many_to_one"}
        relationships["wing_building"] = {
            "entities": ["wing", "building"],
            "cardinality": "many_to_one",
        }
        room_entities.append("wing")
        files["models/wings.yml"] = _model("wings", ["wing", "building"], ["name"], {})
    files["models/rooms.yml"] = _model(
        "rooms",
        room_entities,
        ["kind"],
        {"rooms_area": _sum("Area", "area"), "rooms_count": _count("Rooms", "room_id")},
    )
    files["graph.yml"] = {"graph": {"entities": entities, "relationships": relationships}}
    return files


@pytest.fixture(scope="module")
def campus_roots(tmp_path_factory: pytest.TempPathFactory) -> dict[bool, Path]:
    roots = {}
    for wings in (False, True):
        roots[wings] = root = tmp_path_factory.mktemp("read_from_campus")
        for name, body in _files(wings).items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(body), encoding="utf-8")
        with duckdb.connect(str(root / "campus.duckdb")) as connection:
            connection.execute(SEED)
    return roots


@pytest.fixture()
def campus(campus_roots: dict[bool, Path]) -> Iterator[Callable[..., Runtime]]:
    """A runtime over the campus package (``wings`` adds the second route), serving
    ``change(config)`` when given."""
    runtimes: list[Runtime] = []

    def build(wings: bool = False, change: Callable[[Any], Any] | None = None) -> Runtime:
        runtime = Runtime.from_path(str(campus_roots[wings]))
        runtimes.append(runtime)
        if change is not None:
            runtime._config = change(runtime.package_config)
        return runtime

    yield build
    for runtime in runtimes:
        runtime.close()


def _run(runtime: Runtime, query: dict[str, Any], **arguments: Any) -> dict[str, Any]:
    response = mcp_call(runtime, "execute", {"query": query, **arguments})
    assert response["ok"], response
    return response


AREA_BY_BUILDING = {"select": [ROOMS_AREA], "group_by": [BUILDING_NAME]}


# --- Each value's model, and the route the SQL took to it ---------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # Total room area by building: the area is the rooms table's, the name the buildings'.
        (
            AREA_BY_BUILDING,
            {ROOM: [ROOMS_AREA], BUILDING: [BUILDING_NAME, ROOM_BUILDING]},
        ),
        # Room area by kind in one building: a where field is named like a group_by one.
        (
            {
                "select": [ROOMS_AREA],
                "group_by": [ROOM_KIND],
                "where": [{"field": BUILDING_NAME, "op": "is", "value": "Hall"}],
            },
            {ROOM: [ROOMS_AREA, ROOM_KIND], BUILDING: [BUILDING_NAME, ROOM_BUILDING]},
        ),
        # Booked hours by building: two hops, each named once, in route order.
        (
            {"select": [BOOKED_HOURS], "group_by": [BUILDING_NAME]},
            {BOOKING: [BOOKED_HOURS], BUILDING: [BUILDING_NAME, BOOKING_ROOM, ROOM_BUILDING]},
        ),
        # A metric over two models is named under each.
        (
            {"select": [AREA_PER_BOOKING]},
            {ROOM: [AREA_PER_BOOKING], BOOKING: [AREA_PER_BOOKING]},
        ),
    ],
)
def test_an_answer_names_the_model_of_each_value_and_the_route_it_took(campus, query, expected):
    response = _run(campus(), query)
    assert response["read_from"] == expected
    # It comes first, where an agent reads before the rows.
    assert list(response)[:3] == ["ok", "read_from", "rows"]
    assert "routes" not in response  # each pair has one route: nothing was chosen


def test_the_answer_itself_is_unchanged(campus):
    response = _run(campus(), AREA_BY_BUILDING)
    assert sorted((row[BUILDING_NAME], row["rooms_area"]) for row in response["rows"]) == [
        ("Annex", 50.0),
        ("Hall", 50.0),
    ]


@pytest.mark.parametrize("mode", ["validate", "sql"])
def test_a_call_that_is_only_checked_names_no_models(campus, mode):
    response = mcp_call(campus(), "execute", {"query": AREA_BY_BUILDING, "mode": mode})
    assert response["ok"], response
    assert "read_from" not in response


# --- Nothing the response already states --------------------------------------------------------


def test_a_model_routes_already_names_is_left_out(jaffle_snapshot):
    # Monthly revenue by store: the route from orders to stores was chosen among several, so
    # `routes` names both models and read_from has nothing to add.
    runtime = Runtime.from_path(jaffle_snapshot.source_path)
    try:
        query = {
            "select": ["measure.jaffle.revenue_usd"],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {"grain": "month", "start": "2017-01-01", "end": "2018-01-01"},
        }
        response = _run(runtime, query)
        assert [(row["from"], row["to"]) for row in response["routes"]] == [
            ("entity.jaffle_order", "entity.jaffle_store")
        ]
        assert "read_from" not in response
    finally:
        runtime.close()


def test_a_route_a_fan_out_note_states_is_not_repeated(campus):
    # Rooms with an exam booking: the fan-out note names the hop to the bookings.
    query = {
        "select": [ROOM_COUNT],
        "where": [{"field": PURPOSE, "op": "is", "value": "exam"}],
    }
    response = _run(campus(), query)
    (note,) = [row for row in response["warnings"] if row["code"] == "REWRITE_APPLIED"]
    assert note["details"]["paths"] == {BOOKING: [BOOKING_ROOM]}
    assert response["read_from"] == {ROOM: [ROOM_COUNT], BOOKING: [PURPOSE]}
    assert response["rows"] == [{"rooms_count": 2}]


def test_a_route_a_listing_states_is_not_repeated(tmp_path):
    # Each department with the cities of its rooms' buildings: the listing names its route.
    runtime = Runtime.from_path(str(listings._write_package(tmp_path, traversal=["forward"])))
    try:
        response = _run(runtime, listings.BY_CITY)
        (listed,) = [row for row in response["assumptions"] if row["code"] == "LISTED_THROUGH"]
        department, address = listed["routes"][-1]["from"], listed["routes"][-1]["to"]
        assert response["read_from"] == {
            department: [listings.DEPARTMENT_NAME],
            address: [listings.CITY],
        }
    finally:
        runtime.close()


def test_a_pass_through_note_states_its_route():
    note = {"code": "ROUTE_PASS_THROUGH", "object_ids": [BOOKING, BUILDING], "details": {}}
    assert _stated_routes(Row(measures=[]), {"warnings": [note]}) == {(BOOKING, BUILDING)}


def test_a_model_routes_names_is_kept_while_another_route_into_it_is_unstated(campus):
    # Room area and booked hours by building, where `routes` named only the bookings' route:
    # the bookings are left out, and the rooms' own hop to the building is still said.
    runtime = campus()
    query = {"select": [ROOMS_AREA, BOOKED_HOURS], "group_by": [BUILDING_NAME]}
    payload = runtime.engine_payload(query)
    with runtime.request_scope():
        _, compiled = runtime._enforced_compile(payload, payload["policy_context"])
        config = runtime._config
    booking_route = {"from": BOOKING, "to": BUILDING, "via": [BOOKING_ROOM, ROOM_BUILDING]}
    assert read_from(config, compiled, {"routes": [booking_route]}) == {
        ROOM: [ROOMS_AREA],
        BUILDING: [BUILDING_NAME, ROOM_BUILDING],
    }
    both = [booking_route, {"from": ROOM, "to": BUILDING, "via": [ROOM_BUILDING]}]
    assert read_from(config, compiled, {"routes": both}) == {}


# --- Only what this caller can see -------------------------------------------------------------


WING_OBJECTS = [
    WING,
    "dimension.campus_wing_name",
    "dimension.campus_wing_id",
    "dimension.campus_wing_building_id",
    "dimension.campus_room_wing_id",
    "relationship.room_wing",
    "relationship.wing_building",
]


def test_a_caller_who_cannot_see_a_route_is_told_what_a_package_without_it_says(campus):
    hide = visibility_policy("hidden", WING)

    def hidden(config):
        return with_policies(config, hide)

    author = _run(campus(wings=True, change=hidden), AREA_BY_BUILDING)
    reader = _run(campus(wings=True, change=hidden), AREA_BY_BUILDING, policy_context=CALLER)
    without = _run(
        campus(wings=True, change=lambda config: absent(hidden(config), WING_OBJECTS)),
        AREA_BY_BUILDING,
        policy_context=CALLER,
    )
    # The author sees two routes from a room to its building: `routes` names the one taken.
    assert [row["to"] for row in author["routes"]] == [BUILDING]
    assert "read_from" not in author
    # The reader sees one, which read_from names, exactly as the package without wings does.
    assert reader["read_from"] == {ROOM: [ROOMS_AREA], BUILDING: [BUILDING_NAME, ROOM_BUILDING]}
    assert envelope(reader) == envelope(without)
    assert not any(token in str(reader) for token in ("wing", "Wing"))


def test_a_metric_grant_names_no_model(campus):
    runtime = campus()
    context = RequestContext(metric_allowlist=(AREA_PER_BOOKING,), dimension_allowlist=())
    response = runtime.query({"select": [AREA_PER_BOOKING]}, context=context)
    assert response["ok"], response
    assert "read_from" not in response
    assert "read_from" in runtime.query({"select": [AREA_PER_BOOKING]})


def test_a_route_through_a_relationship_the_view_lacks_is_never_named():
    # read_from reads the caller's view: a relationship it lacks is never named, whatever a
    # route the SQL read (a row filter's, from the whole package) passed through.
    view = Row(
        measures=[],
        metric_recipes=[],
        dimensions=[Row(id=BUILDING_NAME, entity=BUILDING)],
        entities=[Row(id=ROOM), Row(id=BUILDING)],
        relationships=[],
    )
    plan = Row(root_entity=ROOM, selected_paths={BUILDING: [ROOM_BUILDING]}, measure_plans=[])
    compiled = {
        "logical_plan": plan,
        "explain": Row(normalized_query={"group_by": [BUILDING_NAME]}),
        "route_choices": [],
    }
    assert read_from(view, compiled, {}) == {BUILDING: [BUILDING_NAME]}
    view.relationships = [Row(id=ROOM_BUILDING)]
    assert read_from(view, compiled, {}) == {BUILDING: [BUILDING_NAME, ROOM_BUILDING]}
